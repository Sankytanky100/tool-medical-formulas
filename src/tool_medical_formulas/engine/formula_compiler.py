"""
Formula Compiler - Converts formula JSON specs into deterministic calculation functions.

Hard Rule: "LLM chooses, code computes"
- LLM identifies formula_id and asks for inputs
- Deterministic engine validates and computes exactly
"""

from typing import Dict, Any, List, Optional, Callable, Tuple
from pathlib import Path
import copy
import json
import logging
import math
import re
import unicodedata
from pydantic import BaseModel, Field, create_model, ValidationError
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class FormulaSpecError(Exception):
    """Raised when formula spec is invalid."""
    pass


class FormulaCompileTimeout(FormulaSpecError):
    """Compile or math-eval exceeded ``FORMULA_COMPILER_TIMEOUT_SEC`` (LTL-3.2)."""


def formula_compiler_timeout_sec() -> float:
    """Hard wall-clock for latex/math compile + eval. Default 2s; env override."""
    import os

    try:
        raw = float(os.getenv("FORMULA_COMPILER_TIMEOUT_SEC") or "2.0")
    except (TypeError, ValueError):
        raw = 2.0
    return max(0.5, min(raw, 10.0))


def formula_compiler_ai_extract_enabled() -> bool:
    """Opt-in LLM extract. Default off — ``generate_content`` was the ~60s latex hang (not sympy)."""
    import os

    return os.getenv("FORMULA_COMPILER_AI_EXTRACT", "false").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _call_with_timeout(fn, timeout_sec: float, *, label: str):
    """Return ``fn()`` or raise ``FormulaCompileTimeout``. Caller unblocks; worker may linger."""
    import concurrent.futures

    limit = float(timeout_sec)
    if limit <= 0:
        return fn()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    fut = pool.submit(fn)
    try:
        return fut.result(timeout=limit)
    except concurrent.futures.TimeoutError as exc:
        logger.warning("formula_compiler timeout after %.1fs (%s)", limit, label)
        raise FormulaCompileTimeout(
            f"Formula compile/eval timed out after {limit:.1f}s ({label}). "
            "Latex medical_calculator is not executable in this engine; "
            "do not wait on identify/tool_timeout."
        ) from exc
    finally:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            pool.shutdown(wait=False)


def collect_formula_metadata_issues(formula_id: str, spec: Dict[str, Any]) -> tuple[list[str], list[str]]:
    """
    Wave B R19/R20 — non-fatal quality checks for form generation / CI lint.

    Returns ``(warnings, errors)``. Errors are structural problems that should
    block silent deploy when strict lint is enabled.
    """
    warnings: list[str] = []
    errors: list[str] = []
    calc = (spec.get("calculation_type") or spec.get("formula", {}).get("calculation_type") or "").strip().lower()
    if calc == "conversion":
        if not spec.get("conversion") and not spec.get("formula", {}).get("conversion"):
            errors.append(f"{formula_id}: calculation_type=conversion requires `conversion` metadata")
    aliases = spec.get("aliases") or []
    syn = spec.get("clinical_synonyms") or []
    if not aliases and not syn:
        warnings.append(f"{formula_id}: consider adding `aliases` or `clinical_synonyms` for identification (R16)")
    for inp in spec.get("inputs") or []:
        iid = inp.get("id") or "?"
        label = str(inp.get("label") or "").strip()
        if not label:
            warnings.append(f"{formula_id}: input '{iid}' lacks human-readable `label`")
        it = str(inp.get("type") or "").lower()
        if it in ("number", "integer", "float"):
            u = str(inp.get("unit") or "").strip()
            cu = str(inp.get("canonical_unit") or "").strip()
            if not u or not cu:
                warnings.append(
                    f"{formula_id}: numeric input '{iid}' should declare both `unit` and `canonical_unit` for R17"
                )
            cmn, cmx = inp.get("clinical_min"), inp.get("clinical_max")
            if cmn is None or cmx is None:
                warnings.append(
                    f"{formula_id}: numeric input '{iid}' missing `clinical_min`/`clinical_max` (soft plausibility)"
                )
    return warnings, errors


class FormulaCompiler:
    """Compiles formula JSON specs into deterministic calculation functions."""
    
    def __init__(
        self,
        formulas_dir: Optional[str] = None,
        formula_service: Optional[Any] = None,
        use_adk_agents: bool = True,
        model: Optional[Any] = None
    ):
        """
        Initialize Formula Compiler.
        
        Args:
            formulas_dir: Directory containing formula JSON files (fallback)
            formula_service: Optional FormulaService for database access
            use_adk_agents: If True, use ADK agents for parsing (default: True)
            model: Optional Gemini model for AI-powered extraction
        """
        self.formulas_dir = Path(formulas_dir) if formulas_dir else Path("data/formulas")
        self.formula_service = formula_service
        self.use_adk_agents = use_adk_agents
        self._compiled_cache: Dict[str, Dict[str, Any]] = {}
        # Filesystem spec cache only (never FormulaService/DB — retraction/stale risk).
        self._spec_cache: Dict[str, Tuple[Path, float, Dict[str, Any]]] = {}
        self._validation_schemas: Dict[str, BaseModel] = {}
        self._computation_functions: Dict[str, Callable] = {}
        self._compile_failed: Dict[str, str] = {}
        
        # Initialize AI model for formula extraction (if not provided, will be created on demand)
        self._ai_model = model
        self._prompt_manager = None
        try:
            from prompts.prompt_manager import PromptManager
            self._prompt_manager = PromptManager()
        except Exception as e:
            logger.warning(f"Could not initialize PromptManager: {e}")
    
    def load_formula(self, formula_id: str, version: Optional[str] = None) -> Dict[str, Any]:
        """
        Load formula JSON spec from database or file.
        
        Priority:
        1. FormulaService (Firestore + GCS)
        2. Local file system (fallback)
        
        Args:
            formula_id: Formula identifier
            version: Optional version (defaults to latest)
        
        Returns:
            Formula spec dictionary
        
        Raises:
            FormulaSpecError: If formula not found or invalid
        """
        try:
            from tool_medical_formulas.engine.ckd_epi_creatinine import resolve_ckd_epi_formula_id

            aliased = resolve_ckd_epi_formula_id(formula_id)
            if aliased and aliased != formula_id:
                logger.info("FormulaCompiler: alias '%s' -> '%s'", formula_id, aliased)
                formula_id = aliased
        except Exception:
            pass

        # Try FormulaService first (database) with fuzzy matching enabled
        if self.formula_service:
            try:
                # Use fuzzy matching to find the correct formula ID
                formula = self.formula_service.get_formula(formula_id, version, fuzzy_match=True)
                if formula:
                    # Ensure formula_id is set - use the actual ID from database if different
                    actual_id = formula.get('id') or formula.get('formula_id')
                    if actual_id and actual_id != formula_id:
                        logger.info(f"FormulaCompiler: Matched '{formula_id}' to '{actual_id}'")
                        formula['id'] = actual_id
                        formula['formula_id'] = actual_id
                    elif 'id' not in formula and 'formula_id' not in formula:
                        formula['id'] = formula_id
                        formula['formula_id'] = formula_id
                    return formula
            except Exception as e:
                logger.warning(f"Failed to load formula {formula_id} from database: {e}")
                # Fall through to file system
        
        # Fallback to file system (mtime-keyed; callers get a deepcopy)
        cache_key = f"{formula_id}:{version or 'latest'}"
        cached = self._spec_cache.get(cache_key)
        if cached is not None:
            path, mtime, spec = cached
            try:
                if path.is_file() and path.stat().st_mtime == mtime:
                    return copy.deepcopy(spec)
            except OSError:
                pass
            self._spec_cache.pop(cache_key, None)

        possible_files = [
            self.formulas_dir / f"{formula_id}.json",
            self.formulas_dir / f"merged_{formula_id}.json",
            self.formulas_dir / f"unmatched2_{formula_id}.json",
        ]
        
        formula_file = None
        for file_path in possible_files:
            if file_path.exists():
                formula_file = file_path
                break
        
        if not formula_file:
            raise FormulaSpecError(f"Formula '{formula_id}' not found in database or {self.formulas_dir}")
        
        try:
            with open(formula_file, 'r', encoding='utf-8') as f:
                spec = json.load(f)
            
            # Validate basic structure
            if not isinstance(spec, dict):
                raise FormulaSpecError(f"Formula '{formula_id}': Invalid JSON structure")
            
            if 'id' not in spec and 'formula_id' not in spec:
                # Use filename as fallback
                spec['id'] = formula_id

            try:
                mtime = formula_file.stat().st_mtime
            except OSError:
                mtime = 0.0
            self._spec_cache[cache_key] = (formula_file, mtime, spec)
            return copy.deepcopy(spec)
        
        except json.JSONDecodeError as e:
            raise FormulaSpecError(f"Formula '{formula_id}': Invalid JSON - {e}")
        except FormulaSpecError:
            raise
        except Exception as e:
            raise FormulaSpecError(f"Formula '{formula_id}': Error loading - {e}")

    @staticmethod
    def _validate_spec_business_rules(formula_id: str, spec: Dict[str, Any]) -> None:
        """Hard rejects for inconsistent numeric bounds (Wave B R19)."""
        for inp in spec.get("inputs") or []:
            iid = inp.get("id") or "?"
            vr = inp.get("validation") or {}
            mn, mx = vr.get("min"), vr.get("max")
            if mn is not None and mx is not None:
                try:
                    if float(mn) > float(mx):
                        raise FormulaSpecError(
                            f"Formula '{formula_id}' input '{iid}': validation.min > validation.max"
                        )
                except FormulaSpecError:
                    raise
                except (TypeError, ValueError):
                    pass
            cmn, cmx = inp.get("clinical_min"), inp.get("clinical_max")
            if cmn is not None and cmx is not None:
                try:
                    if float(cmn) > float(cmx):
                        raise FormulaSpecError(
                            f"Formula '{formula_id}' input '{iid}': clinical_min > clinical_max"
                        )
                except FormulaSpecError:
                    raise
                except (TypeError, ValueError):
                    pass

    @staticmethod
    def _clinical_plausibility_warnings(
        inputs: List[Dict[str, Any]],
        input_dict: Dict[str, Any],
        formula_id: str,
    ) -> List[Dict[str, Any]]:
        """Soft warnings when syntactic validation passes but value is clinically odd (R19)."""
        warns: List[Dict[str, Any]] = []
        for inp in inputs or []:
            iid = inp.get("id")
            if not iid or iid not in input_dict:
                continue
            raw = input_dict[iid]
            if isinstance(raw, bool):
                continue
            try:
                val = float(raw) if isinstance(raw, (int, float)) else float(str(raw).strip())
            except (TypeError, ValueError):
                continue
            cmin = inp.get("clinical_min")
            cmax = inp.get("clinical_max")
            if cmin is None or cmax is None:
                continue
            try:
                lo = float(cmin)
                hi = float(cmax)
            except (TypeError, ValueError):
                continue
            if lo > hi:
                continue
            vr = inp.get("validation") or {}
            try:
                smin = float(vr["min"]) if vr.get("min") is not None else None
                smax = float(vr["max"]) if vr.get("max") is not None else None
            except (TypeError, ValueError):
                smin = smax = None
            in_syn = True
            if smin is not None and val < smin - 1e-9:
                in_syn = False
            if smax is not None and val > smax + 1e-9:
                in_syn = False
            if not in_syn:
                continue
            if val < lo - 1e-9 or val > hi + 1e-9:
                warns.append(
                    {
                        "input_id": iid,
                        "value": val,
                        "clinical_min": lo,
                        "clinical_max": hi,
                        "message": (
                            f"Value {val} for '{iid}' is outside typical clinical range "
                            f"[{lo}, {hi}] (syntactic bounds still pass)."
                        ),
                    }
                )
        return warns

    def compile_formula(self, formula_id: str, spec: Optional[Dict[str, Any]] = None, version: Optional[str] = None) -> Dict[str, Any]:
        """
        Compile formula spec into validation schema and computation function.
        
        Args:
            formula_id: Formula identifier
            spec: Optional formula spec (if not provided, will load from file)
        
        Returns:
            Dictionary with:
            - validation_schema: Pydantic model class
            - compute_function: Callable computation function
            - metadata: Compilation metadata
        """
        # Check cache first (with version)
        cache_key = f"{formula_id}:{version or 'latest'}"
        if cache_key in self._compiled_cache:
            logger.debug(f"Using cached compilation for '{formula_id}'")
            return self._compiled_cache[cache_key]
        fail_reason = self._compile_failed.get(cache_key)
        if fail_reason:
            raise FormulaCompileTimeout(
                f"Formula '{formula_id}' previously failed compile ({fail_reason}); "
                "not retrying latex/math compile this process."
            )
        
        # Load spec if not provided
        if spec is None:
            spec = self.load_formula(formula_id, version)

        # Extract formula_id from spec
        formula_id = spec.get('id') or spec.get('formula_id') or formula_id

        self._validate_spec_business_rules(formula_id, spec)
        
        # Get version from spec or parameter
        formula_version = version or spec.get('version') or spec.get('metadata', {}).get('version', '1.0.0')
        
        timeout = formula_compiler_timeout_sec()

        def _compile_body():
            schema = self._generate_validation_schema(formula_id, spec)
            compute = self._generate_compute_function(formula_id, spec, schema)
            return schema, compute

        try:
            validation_schema, compute_function = _call_with_timeout(
                _compile_body,
                timeout,
                label=f"compile:{formula_id}",
            )
        except FormulaCompileTimeout:
            self._compile_failed[cache_key] = "timeout"
            raise
        
        # Compile metadata
        metadata = {
            "formula_id": formula_id,
            "formula_name": spec.get('name', formula_id),
            "version": formula_version,
            "compiled_at": datetime.now(timezone.utc).isoformat(),
            "calculation_type": self._determine_calculation_type(spec)
        }
        
        try:
            from tool_medical_formulas.ops_metrics import incr_counter

            for inp in spec.get("inputs") or []:
                u = str(inp.get("unit") or "").strip()
                cu = str(inp.get("canonical_unit") or "").strip()
                if (u and not cu) or (cu and not u):
                    incr_counter("missing_unit_metadata_total")
                    break
        except Exception:
            pass

        # Cache compiled formula
        compiled = {
            "validation_schema": validation_schema,
            "compute_function": compute_function,
            "metadata": metadata,
            "spec": spec
        }
        
        # Cache with version-aware key
        self._compiled_cache[cache_key] = compiled
        self._validation_schemas[cache_key] = validation_schema
        self._computation_functions[cache_key] = compute_function
        
        logger.info(f"Compiled formula '{formula_id}' successfully")
        
        return compiled
    
    def _generate_validation_schema(self, formula_id: str, spec: Dict[str, Any]) -> BaseModel:
        """
        Generate Pydantic validation schema from formula spec.
        
        Uses AI agent to intelligently extract required variables when inputs array is incomplete.
        
        Args:
            formula_id: Formula identifier
            spec: Formula spec dictionary
        
        Returns:
            Pydantic BaseModel class for input validation
        """
        # Prefer catalog inputs — AI analysis is optional fill-in only.
        required_variables: list = []
        inputs_present = bool(spec.get("inputs"))
        if not inputs_present:
            try:
                import importlib.util

                if importlib.util.find_spec("agents.formula_analysis_agent") is None:
                    logger.debug(
                        "formula_analysis_agent unavailable; using inputs array for %s",
                        formula_id,
                    )
                else:
                    from agents.formula_analysis_agent import FormulaAnalysisAgent

                    analysis_agent = FormulaAnalysisAgent()
                    logger.info("Using AI agent to analyze formula %s", formula_id)
                    analysis = analysis_agent.analyze_formula_sync(formula_id, spec)
                    required_variables = analysis.get("required_variables", []) or []
                    logger.info(
                        "AI agent extracted %s required variables for %s",
                        len(required_variables),
                        formula_id,
                    )
            except Exception as e:
                logger.warning(
                    "AI analysis failed for %s: %s, falling back to inputs array",
                    formula_id,
                    e,
                )
                required_variables = []
        
        # Build field definitions for Pydantic model
        field_definitions = {}
        field_annotations = {}
        
        # Use AI-extracted variables if available, otherwise fall back to inputs array
        if required_variables:
            # Use AI-extracted variables
            for var_def in required_variables:
                var_id = var_def.get('id')
                if not var_id:
                    continue
                
                var_type = var_def.get('type', 'float')
                required = var_def.get('required', True)
                description = var_def.get('description', '')
                
                # Map type string to Python type
                if var_type in ['float', 'number']:
                    field_type = float
                    field = Field(
                        ... if required else None,
                        description=description
                    )
                elif var_type == 'int' or var_type == 'integer':
                    field_type = int
                    field = Field(
                        ... if required else None,
                        description=description
                    )
                elif var_type in ['boolean', 'bool']:
                    field_type = bool
                    field = Field(
                        ... if required else False,
                        description=description
                    )
                else:
                    field_type = str
                    field = Field(
                        ... if required else None,
                        description=description
                    )
                
                field_definitions[var_id] = (field_type, field)
                field_annotations[var_id] = field_type
        
        # Also add fields from inputs array (in case AI missed something or for backward compatibility)
        inputs = spec.get('inputs', [])
        for input_def in inputs:
            input_id = input_def.get('id')
            if not input_id:
                logger.warning(f"Formula '{formula_id}': Input missing 'id', skipping")
                continue
            
            input_type = input_def.get('type', 'text')
            required = input_def.get('required', True)
            validation = input_def.get('validation', {})
            
            # Determine Python type and Pydantic field
            if input_type in ['number', 'integer', 'float']:
                field_type = float if input_type == 'float' else int
                field = Field(
                    ... if required else None,
                    ge=validation.get('min'),
                    le=validation.get('max'),
                    description=input_def.get('description', input_def.get('label', ''))
                )
            elif input_type in ['boolean', 'checkbox']:
                field_type = bool
                field = Field(
                    ... if required else False,
                    description=input_def.get('description', input_def.get('label', ''))
                )
            elif input_type in ['select', 'radio', 'text']:
                # For select/radio, validate against options if provided
                options = input_def.get('options', [])
                if options:
                    # Extract valid values
                    valid_values = [opt.get('value') for opt in options if opt.get('value')]
                    field_type = str
                    field = Field(
                        ... if required else None,
                        description=input_def.get('description', input_def.get('label', '')),
                        # Note: Enum validation would be better, but keeping simple for now
                    )
                else:
                    field_type = str
                    field = Field(
                        ... if required else None,
                        description=input_def.get('description', input_def.get('label', ''))
                    )
            else:
                # Default to string
                field_type = str
                field = Field(
                    ... if required else None,
                    description=input_def.get('description', input_def.get('label', ''))
                )
            
            field_definitions[input_id] = (field_type, field)
            field_annotations[input_id] = field_type
        
        # Create Pydantic model dynamically
        model_name = f"{formula_id.replace('-', '_').title()}Inputs"
        
        # Build field definitions for create_model
        # Format: field_name: (type, Field(...))
        model_fields = {}
        for field_name, (field_type, field) in field_definitions.items():
            model_fields[field_name] = (field_type, field)
        
        # Create model (Pydantic v2 doesn't use __config__, uses model_config class var instead)
        # We'll set extra='forbid' via model_config after creation
        validation_schema = create_model(
            model_name,
            __base__=BaseModel,
            __module__=__name__,
            **model_fields
        )
        
        # Set model config for extra='forbid' (Pydantic v2 style)
        try:
            from pydantic import ConfigDict
            validation_schema.model_config = ConfigDict(extra='forbid')
        except ImportError:
            # Fallback for older Pydantic versions
            class Config:
                extra = 'forbid'
            validation_schema.__config__ = Config
        
        return validation_schema
    
    def _determine_calculation_type(self, spec: Dict[str, Any]) -> str:
        """
        Determine calculation type from formula spec.
        
        Returns:
            'points_sum', 'mathematical', or 'conversion'
        """
        formula_id = spec.get('id', '').lower()
        
        # Check if it's a points-based scoring system FIRST (highest priority)
        inputs = spec.get('inputs', [])
        has_points = any(
            any(opt.get('points') is not None for opt in input_def.get('options', []))
            for input_def in inputs
        )
        
        if has_points:
            return 'points_sum'
        
        # Known mathematical formulas (by ID)
        if any(keyword in formula_id for keyword in ['meld', 'fatty-liver', 'fli', 'saps', 'apache']):
            return 'mathematical'
        
        # Explicit conversion formulas (metadata or structured hints)
        if spec.get('calculation_type') == 'conversion':
            return 'conversion'
        formula_section = spec.get('formula', {})
        if formula_section.get('calculation_type') == 'conversion':
            return 'conversion'
        if spec.get('conversion') or formula_section.get('conversion'):
            return 'conversion'

        # Check formula section for calculation type hints
        # Check for mathematical expressions
        if formula_section.get('latexFormulas') or formula_section.get('formulaText'):
            # Check if it contains mathematical operations
            formula_text = str(formula_section.get('formulaText', ''))
            raw_text = str(formula_section.get('rawText', ''))
            combined_text = formula_text + ' ' + raw_text
            
            # Strong indicators of mathematical formula
            if any(indicator in combined_text.lower() for indicator in ['ln(', 'log(', 'exp(', 'e^']):
                return 'mathematical'
            
            # Check if it contains mathematical operations (but not just addition)
            if any(op in combined_text for op in ['*', '/', '^', '**', 'ln(', 'log(']):
                return 'mathematical'
        
        # Default to points_sum for scoring systems
        return 'points_sum'
    
    def _generate_compute_function(
        self,
        formula_id: str,
        spec: Dict[str, Any],
        validation_schema: BaseModel,
        structured_spec: Optional[Dict[str, Any]] = None
    ) -> Callable:
        """
        Generate deterministic computation function.
        
        Args:
            formula_id: Formula identifier
            spec: Formula spec dictionary
            validation_schema: Pydantic validation schema
            structured_spec: Optional structured spec from ADK parser agent
        
        Returns:
            Callable computation function
        """
        calculation_type = self._determine_calculation_type(spec)
        inputs = spec.get('inputs', [])
        
        # Use structured spec if available (from ADK agent)
        if structured_spec and 'calculation_type' in structured_spec:
            calculation_type = structured_spec['calculation_type']
        
        if calculation_type == 'points_sum':
            return self._create_points_sum_function(formula_id, spec, validation_schema, inputs)
        elif calculation_type == 'mathematical':
            return self._create_mathematical_function(
                formula_id, spec, validation_schema, inputs, structured_spec=structured_spec
            )
        elif calculation_type == 'conversion':
            return self._create_conversion_function(formula_id, spec, validation_schema, inputs)
        else:
            raise FormulaSpecError(f"Formula '{formula_id}': Unsupported calculation type '{calculation_type}'")
    
    def _create_points_sum_function(
        self,
        formula_id: str,
        spec: Dict[str, Any],
        validation_schema: BaseModel,
        inputs: List[Dict[str, Any]]
    ) -> Callable:
        """Create points sum computation function."""
        
        def compute(inputs_dict: Dict[str, Any]) -> Dict[str, Any]:
            # Validate inputs
            try:
                validated_inputs = validation_schema(**inputs_dict)
            except ValidationError as e:
                raise ValueError(f"Input validation failed: {e}")
            
            # Calculate points sum
            total_points = 0
            points_breakdown = {}
            
            # Convert validated inputs to dict
            input_dict = validated_inputs.model_dump()
            input_dict = FormulaCompiler._canonicalize_input_magnitudes(
                inputs, input_dict, formula_id
            )
            
            for input_def in inputs:
                input_id = input_def.get('id')
                if not input_id:
                    continue
                
                input_value = input_dict.get(input_id)
                
                # Get points from options
                options = input_def.get('options', [])
                points = 0
                
                if options:
                    # Find matching option with robust value matching
                    matched = False
                    for opt in options:
                        opt_value = opt.get('value')
                        opt_points = opt.get('points')
                        opt_label = opt.get('label', '')
                        
                        # Helper function to extract numeric value from string
                        def extract_numeric_value(value_str):
                            """Extract numeric value from string, handling formats like '+1', '1', '+1.5', etc."""
                            import re
                            if value_str is None:
                                return None
                            # Try to extract number (handles +1, -1, 1, +1.5, etc.)
                            match = re.search(r'([+-]?\d+\.?\d*)', str(value_str))
                            if match:
                                try:
                                    return float(match.group(1))
                                except:
                                    return None
                            return None
                        
                        # Try exact string match first
                        if str(opt_value) == str(input_value):
                            matched = True
                            if opt_points is not None:
                                points = opt_points
                            else:
                                # Try to extract from label
                                label_points = extract_numeric_value(opt_label)
                                if label_points is not None:
                                    points = label_points
                                else:
                                    points = 0
                            break
                        
                        # Fallback: Try numeric value matching (handles "+1" vs "1" vs 1)
                        input_num = extract_numeric_value(input_value)
                        opt_num = extract_numeric_value(opt_value)
                        
                        if input_num is not None and opt_num is not None:
                            if abs(input_num - opt_num) < 0.001:  # Float comparison with tolerance
                                matched = True
                                if opt_points is not None:
                                    points = opt_points
                                else:
                                    # Use the numeric value itself
                                    points = input_num
                                break
                        
                        # Additional fallback: Check if input value matches label
                        if opt_label and input_value:
                            label_num = extract_numeric_value(opt_label)
                            if input_num is not None and label_num is not None:
                                if abs(input_num - label_num) < 0.001:
                                    matched = True
                                    if opt_points is not None:
                                        points = opt_points
                                    else:
                                        points = input_num
                                    break
                    
                    # If no match found, try boolean/yes-no conversion
                    if not matched and input_value is not None:
                        import logging
                        logger = logging.getLogger(__name__)
                        
                        # Try boolean/yes-no to 0/1 conversion
                        normalized_value = None
                        if isinstance(input_value, str):
                            input_lower = input_value.lower().strip()
                            if input_lower in ['yes', 'true', '1', 'y']:
                                normalized_value = '1'
                            elif input_lower in ['no', 'false', '0', 'n']:
                                normalized_value = '0'
                        
                        # Try again with normalized value
                        if normalized_value:
                            for opt in options:
                                opt_value = opt.get('value')
                                if str(opt_value) == normalized_value:
                                    matched = True
                                    opt_points = opt.get('points')
                                    if opt_points is not None:
                                        points = opt_points
                                    else:
                                        points = float(normalized_value)
                                    logger.info(f"Matched '{input_value}' -> '{normalized_value}' for {input_id}")
                                    break
                        
                        if not matched:
                            logger.warning(
                                f"No matching option found for input '{input_id}' with value '{input_value}'. "
                                f"Available options: {[opt.get('value') for opt in options]}"
                            )
                            # Try to extract numeric value directly from input as last resort
                            input_num = extract_numeric_value(input_value)
                            if input_num is not None:
                                points = input_num
                                logger.info(f"Using extracted numeric value {input_num} for {input_id}")
                            else:
                                points = 0
                else:
                    # Direct points value (for boolean inputs or direct numeric inputs)
                    if isinstance(input_value, bool):
                        points = 1 if input_value else 0
                    elif isinstance(input_value, (int, float)):
                        points = float(input_value)  # Use value as points
                    elif isinstance(input_value, str):
                        # Try to extract numeric value from string
                        import re
                        match = re.search(r'([+-]?\d+\.?\d*)', input_value)
                        if match:
                            try:
                                points = float(match.group(1))
                            except:
                                points = 0
                        else:
                            points = 0
                    else:
                        points = 0
                
                total_points += points
                points_breakdown[input_id] = points
            
            # Determine risk level or category based on score
            # This is formula-specific, so we'll need to extract from spec
            result = {
                "score": total_points,
                "points_breakdown": points_breakdown,
                "formula_id": formula_id,
                "formula_name": spec.get('name', formula_id)
            }
            
            # Add interpretation if available
            interpretation = self._get_interpretation(spec, total_points)
            if interpretation:
                result["interpretation"] = interpretation

            cw = FormulaCompiler._clinical_plausibility_warnings(inputs, input_dict, formula_id)
            if cw:
                result["clinical_warnings"] = cw

            return result
        
        return compute
    
    def _create_mathematical_function(
        self,
        formula_id: str,
        spec: Dict[str, Any],
        validation_schema: BaseModel,
        inputs: List[Dict[str, Any]],
        structured_spec: Optional[Dict[str, Any]] = None
    ) -> Callable:
        """Create mathematical formula computation function."""
        
        # Use structured spec from ADK agent if available
        if structured_spec and 'steps' in structured_spec:
            steps = structured_spec.get('steps', [])
            variable_mappings = structured_spec.get('variable_mappings', {})
            constraints = structured_spec.get('constraints', [])
            conditionals = structured_spec.get('conditionals', [])
            min_max = structured_spec.get('min_max', [])
        else:
            # Fallback to extracting from spec
            formula_section = spec.get('formula', {})
            formula_text = formula_section.get('formulaText', '')
            raw_text = formula_section.get('rawText', '')
            
            # Try to parse steps from formula text
            try:
                from tool_medical_formulas.engine.formula_parser import FormulaParser
                parser = FormulaParser()
                temp_spec = parser.parse_formula_spec(spec)
                steps = [
                    {"variable": s.variable, "expression": s.expression, "description": s.description}
                    for s in temp_spec.steps
                ]
                variable_mappings = temp_spec.variable_mappings
                constraints = [
                    {"variable": c.variable, "condition": c.condition, "error_message": c.error_message}
                    for c in temp_spec.constraints
                ]
                conditionals = [
                    {"condition": c.condition, "actions": c.actions}
                    for c in temp_spec.conditionals
                ]
                min_max = [
                    {"variable": m.variable, "min": m.min, "max": m.max}
                    for m in temp_spec.min_max
                ]
            except Exception as e:
                logger.warning(f"Could not parse formula spec: {e}, using fallback")
                steps = []
                variable_mappings = {}
                constraints = []
                conditionals = []
                min_max = []
        
        # Build input mapping: field_id -> variable_name
        input_mapping = variable_mappings.copy()
        for input_def in inputs:
            input_id = input_def.get('id')
            input_name = input_def.get('name', input_id)
            if input_id and input_id not in input_mapping:
                input_mapping[input_id] = input_name
        
        def compute(inputs_dict: Dict[str, Any]) -> Dict[str, Any]:
            # Validate inputs
            try:
                validated_inputs = validation_schema(**inputs_dict)
            except ValidationError as e:
                raise ValueError(f"Input validation failed: {e}")
            
            # Convert validated inputs to dict for formula evaluation
            input_values = validated_inputs.model_dump()
            input_values = FormulaCompiler._canonicalize_input_magnitudes(
                inputs, input_values, formula_id
            )
            
            # Map input IDs to their values
            variables = {}
            for input_id, var_name in input_mapping.items():
                if input_id in input_values:
                    value = input_values[input_id]
                    # Convert to float if numeric
                    if isinstance(value, (int, float)):
                        variables[var_name] = float(value)
                    elif isinstance(value, str):
                        try:
                            variables[var_name] = float(value)
                        except ValueError:
                            variables[var_name] = value
                    else:
                        variables[var_name] = value
            
            # Try to evaluate formula
            try:
                result = self._evaluate_formula(
                    formula_id,
                    formula_text or raw_text,
                    variables,
                    spec
                )
                
                # Build result dictionary
                result_dict = {
                    "result": result,
                    "formula_id": formula_id,
                    "formula_name": spec.get('name', formula_id),
                    "input_values": variables,
                    "calculation_type": "mathematical"
                }
                
                # Add interpretation if available
                interpretation = self._get_interpretation(spec, result)
                if interpretation:
                    result_dict["interpretation"] = interpretation

                cw = FormulaCompiler._clinical_plausibility_warnings(inputs, input_values, formula_id)
                if cw:
                    result_dict["clinical_warnings"] = cw

                return result_dict
                
            except Exception as e:
                raise ValueError(
                    f"Failed to evaluate mathematical formula for '{formula_id}': {str(e)}"
                )
        
        return compute
    
    def _evaluate_formula(
        self,
        formula_id: str,
        formula_text: str,
        variables: Dict[str, float],
        spec: Dict[str, Any]
    ) -> float:
        """
        Evaluate mathematical formula from text using AI-powered extraction.
        
        Uses AI to intelligently extract the mathematical expression and map variables,
        replacing hardcoded parsing logic.
        """
        # Handle known formulas with special logic (keep for performance)
        if 'fatty-liver-index' in formula_id.lower() or 'fli' in formula_id.lower():
            return self._calculate_fatty_liver_index(variables)
        elif 'meld' in formula_id.lower():
            return self._calculate_meld_score(variables, spec)
        elif 'ckd' in formula_id.lower() and ('epi' in formula_id.lower() or 'gfr' in formula_id.lower()):
            from tool_medical_formulas.engine.ckd_epi_creatinine import (
                try_calculate_ckd_epi_from_inputs,
            )

            out = try_calculate_ckd_epi_from_inputs(variables)
            if out.get("status") == "success":
                return float(out["result"])
            raise ValueError(out.get("error") or "ckd_epi_inputs_incomplete")
        elif 'saps' in formula_id.lower():
            # SAPS is complex - would need full implementation
            raise NotImplementedError(f"SAPS calculation requires full implementation")

        def _eval_body() -> float:
            # Default: regex fallback only. AI extract calls generate_content and
            # was the latex-class ~60s hang (not a sympy loop). LTL-3.2 timeout
            # still wraps this body if AI extract is opted in.
            if formula_compiler_ai_extract_enabled():
                extracted = self._extract_formula_with_ai(
                    formula_id, formula_text, list(variables.keys())
                )
            else:
                extracted = self._extract_mathematical_expression_fallback(
                    formula_text, list(variables.keys())
                )
            if not extracted or not str(extracted.get("normalized_expression") or "").strip():
                raise FormulaSpecError(
                    f"Formula '{formula_id}' has no executable expression in this engine "
                    "(latex/prose medical_calculator). Do not invent coefficients."
                )

            formula = extracted["normalized_expression"]
            var_mappings = extracted.get("variable_mappings", {})

            for original_var, normalized_var in var_mappings.items():
                if normalized_var in variables:
                    pattern = r"\b" + re.escape(original_var) + r"\b"
                    formula = re.sub(pattern, normalized_var, formula, flags=re.IGNORECASE)

            for var_name, var_value in variables.items():
                pattern = r"\b" + re.escape(var_name.lower()) + r"\b"
                formula = re.sub(pattern, str(var_value), formula, flags=re.IGNORECASE)

            formula = re.sub(r"\bln\s*\(", "math.log(", formula)
            formula = re.sub(r"\blog\s*\(", "math.log10(", formula)
            formula = re.sub(r"\bexp\s*\(", "math.exp(", formula)
            formula = re.sub(r"\bsqrt\s*\(", "math.sqrt(", formula)
            formula = re.sub(r"\bpi\b", "math.pi", formula)
            formula = re.sub(r"\be\b", "math.e", formula)
            formula = formula.replace("^", "**")
            formula = unicodedata.normalize("NFKC", formula)
            result = eval(formula, {"__builtins__": {}, "math": math}, {})
            return float(result)

        try:
            return _call_with_timeout(
                _eval_body,
                formula_compiler_timeout_sec(),
                label=f"eval:{formula_id}",
            )
        except FormulaCompileTimeout:
            raise
        except Exception as e:
            raise ValueError(
                f"Could not parse formula: {formula_text[:100]}... Error: {str(e)}"
            )
    
    def _extract_formula_with_ai(
        self,
        formula_id: str,
        formula_text: str,
        available_variables: List[str]
    ) -> Dict[str, Any]:
        """
        Use AI to intelligently extract mathematical expression from formula text.
        
        This replaces hardcoded regex-based extraction with AI-powered analysis.
        
        Args:
            formula_id: Formula identifier
            formula_text: Raw formula text (may contain examples, descriptions, etc.)
            available_variables: List of available variable names from inputs
        
        Returns:
            Dictionary with extracted expression and variable mappings
        """
        try:
            # Use AI model if available
            if not self._ai_model:
                try:
                    from google.genai import GenerativeModel
                    self._ai_model = GenerativeModel("gemini-1.5-flash")
                except ImportError:
                    try:
                        from google.adk.models import Gemini
                        self._ai_model = Gemini(model_name="gemini-1.5-flash")
                    except ImportError:
                        logger.warning("Could not import AI model, falling back to regex extraction")
                        return self._extract_mathematical_expression_fallback(formula_text, available_variables)
            
            if not self._prompt_manager:
                from prompts.prompt_manager import PromptManager
                self._prompt_manager = PromptManager()
            
            # Get prompt template
            prompt = self._prompt_manager.get_templated_prompt(
                agent_name="formula_extraction",
                prompt_name="main_prompt",
                variables={
                    "formula_id": formula_id,
                    "formula_text": formula_text[:2000],  # Limit length
                    "available_variables": json.dumps(available_variables, indent=2)
                }
            )
            
            # Call AI model (sync call since this is used in sync context)
            # Use a simple synchronous approach for formula extraction
            try:
                # Generate content synchronously
                # Handle both GenerativeModel and Gemini model interfaces
                if hasattr(self._ai_model, 'generate_content'):
                    response = self._ai_model.generate_content(prompt)
                elif hasattr(self._ai_model, 'generate'):
                    response = self._ai_model.generate(prompt)
                else:
                    raise ValueError("AI model does not support generate_content or generate")
                
                response_text = response.text if hasattr(response, 'text') else str(response)
                
                # Parse JSON response
                from utils.adk_result_parser import ADKResultParser
                parsed = ADKResultParser.parse_response(response_text)
                
                return {
                    "extracted_expression": parsed.get("extracted_expression", ""),
                    "variable_mappings": parsed.get("variable_mappings", {}),
                    "normalized_expression": parsed.get("normalized_expression", ""),
                    "confidence": parsed.get("confidence", 0.8)
                }
            except Exception as e:
                logger.warning(f"AI model call failed: {e}, falling back to regex extraction")
                return self._extract_mathematical_expression_fallback(formula_text, available_variables)
                
        except Exception as e:
            logger.warning(f"AI extraction failed: {e}, falling back to regex extraction")
            return self._extract_mathematical_expression_fallback(formula_text, available_variables)
    
    def _extract_mathematical_expression_fallback(
        self,
        formula_text: str,
        available_variables: List[str]
    ) -> Dict[str, Any]:
        """
        Fallback regex-based extraction (used when AI fails).
        
        This is a simplified version that extracts basic patterns.
        """
        if not formula_text:
            return {"normalized_expression": "", "variable_mappings": {}}
        
        # Use the existing regex-based extraction as fallback
        extracted = self._extract_mathematical_expression(formula_text)
        
        if not extracted:
            return {"normalized_expression": "", "variable_mappings": {}}
        
        # Extract expression part
        if '=' in extracted:
            parts = extracted.split('=', 1)
            expression = parts[1].strip() if len(parts) == 2 else extracted
        else:
            expression = extracted
        
        # Simple variable mapping (match available variables)
        var_mappings = {}
        expression_lower = expression.lower()
        
        for var in available_variables:
            var_lower = var.lower()
            # Try to find variable in expression
            var_patterns = [
                var_lower,
                var_lower.replace('_', ' '),
                var_lower.replace('_', '-'),
                ' '.join(word.capitalize() for word in var_lower.replace('_', ' ').split())
            ]
            
            for pattern in var_patterns:
                if re.search(r'\b' + re.escape(pattern) + r'\b', expression, re.IGNORECASE):
                    var_mappings[pattern] = var_lower
                    break
        
        return {
            "extracted_expression": expression,
            "variable_mappings": var_mappings,
            "normalized_expression": expression.lower(),
            "confidence": 0.6
        }
    
    def _extract_mathematical_expression(self, formula_text: str) -> str:
        """
        Extract only the mathematical expression from formula text.
        
        Formula text may contain:
        - The actual formula: "Result = expression"
        - Example calculations: "Result = 290 - (2 * (10 + 5))"
        - Descriptive text: "Facts figures causes of..."
        
        This method extracts only the first valid mathematical expression.
        
        Args:
            formula_text: Raw formula text that may contain extra content
        
        Returns:
            Clean mathematical expression (e.g., "result = var1 - (2 * (var2 + var3))")
        """
        if not formula_text:
            return ""
        
        # Split by newlines and process each line
        lines = formula_text.split('\n')
        
        # Look for the first line that looks like a mathematical formula
        # Pattern: variable = expression (where expression contains math operators)
        formula_pattern = r'^([A-Za-z_][A-Za-z0-9_\s]*)\s*=\s*([^=]+)$'
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Skip lines that are clearly not formulas
            # - Too long (likely descriptive text)
            if len(line) > 200:
                continue
            
            # - Contains common descriptive words
            descriptive_words = ['fact', 'figure', 'cause', 'include', 'characterize', 'should', 'usually', 'often', 'rare', 'common']
            if any(word in line.lower() for word in descriptive_words):
                continue
            
            # - Contains units in brackets (likely an example: "290[mOsm/kg]")
            if re.search(r'\d+\[', line):
                continue
            
            # Try to match formula pattern
            match = re.match(formula_pattern, line, re.IGNORECASE)
            if match:
                var_name = match.group(1).strip()
                expression = match.group(2).strip()
                
                # Check if expression contains math operators (likely a formula)
                if re.search(r'[\+\-\*\/\(\)]', expression):
                    # Extract just the expression part (right side of =)
                    # Remove any trailing descriptive text
                    # Stop at common sentence endings or descriptive markers
                    expression = re.split(r'[\.;]\s*(?:fact|figure|cause|include|characterize)', expression, flags=re.IGNORECASE)[0]
                    expression = expression.strip()
                    
                    if expression:
                        return f"{var_name.lower().replace(' ', '_')} = {expression}"
        
        # If no clear formula pattern found, try to extract first mathematical expression
        # Look for patterns like: "something = something - something" or "something = something * something"
        math_expr_pattern = r'([A-Za-z_][A-Za-z0-9_\s]*)\s*=\s*([^=]+?)(?:\s*[\.;]|\s*$|fact|figure|cause)'
        match = re.search(math_expr_pattern, formula_text, re.IGNORECASE | re.MULTILINE)
        if match:
            var_name = match.group(1).strip()
            expression = match.group(2).strip()
            
            # Remove units and example values
            expression = re.sub(r'\d+\[[^\]]+\]', '', expression)  # Remove "290[mOsm/kg]"
            expression = re.sub(r'\d+\s*[a-z]+/[a-z]+', '', expression, flags=re.IGNORECASE)  # Remove "290 mOsm/kg"
            
            # Check if it looks like a formula
            if re.search(r'[\+\-\*\/\(\)]', expression) and len(expression) < 200:
                return f"{var_name.lower().replace(' ', '_')} = {expression.strip()}"
        
        # Last resort: try to find any line with math operators that's not too long
        for line in lines:
            line = line.strip()
            if 10 < len(line) < 150:  # Reasonable length for a formula
                if re.search(r'=\s*[^=]*[\+\-\*\/\(\)]', line):
                    # Remove descriptive text after the formula
                    line = re.split(r'[\.;]\s*(?:fact|figure|cause)', line, flags=re.IGNORECASE)[0]
                    return line.strip()
        
        # If nothing found, return original (will fail later with better error)
        return formula_text
    
    def _calculate_fatty_liver_index(self, variables: Dict[str, float]) -> float:
        """Calculate Fatty Liver Index (FLI)."""
        # FLI = (e^y / (1 + e^y)) * 100
        # y = 0.953 * ln(triglycerides) + 0.139 * BMI + 0.718 * ln(GGT) + 0.053 * waist_circumference - 15.745
        
        # Map variable names (handle different possible names)
        trig = variables.get('trig') or variables.get('triglycerides') or variables.get('triglycerides_mg_dl', 0)
        bmi = variables.get('bmi', 0)
        ggt = variables.get('ggt', 0)
        waist = variables.get('circ') or variables.get('waist_circumference') or variables.get('waist', 0)
        
        if trig <= 0 or ggt <= 0:
            raise ValueError("Triglycerides and GGT must be > 0 for FLI calculation")
        
        # Calculate y
        y = (0.953 * math.log(trig) + 
             0.139 * bmi + 
             0.718 * math.log(ggt) + 
             0.053 * waist - 
             15.745)
        
        # Calculate FLI
        fli = (math.exp(y) / (1 + math.exp(y))) * 100
        
        return round(fli, 2)
    
    def _calculate_meld_score(self, variables: Dict[str, float], spec: Dict[str, Any]) -> float:
        """Calculate MELD Score (pre-2016)."""
        # MELD = (0.957 * ln(Cr) + 0.378 * ln(Bilirubin) + 1.120 * ln(INR) + 0.643) * 10
        
        # Helper to convert to float
        def to_float(val):
            if isinstance(val, (int, float)):
                return float(val)
            if isinstance(val, str):
                try:
                    return float(val)
                except:
                    return 0.0
            return 0.0
        
        # Check for hemodialysis
        inputs = spec.get('inputs', [])
        dialysis_input = None
        for inp in inputs:
            if 'dialysis' in inp.get('id', '').lower():
                dialysis_input = inp.get('id')
                break
        
        # Get creatinine value
        cr = to_float(variables.get('creatinine', 0))
        if dialysis_input:
            dialysis_val = variables.get(dialysis_input)
            # Check if dialysis is "1" or True
            if str(dialysis_val).lower() in ['1', 'true', 'yes']:
                cr = 4.0  # Set to 4.0 for hemodialysis
        
        # Ensure minimum values of 1.0
        cr = max(1.0, cr)
        bilirubin = max(1.0, to_float(variables.get('bilirubin', 1.0)))
        inr = max(1.0, to_float(variables.get('inr', 1.0)))
        
        # Calculate MELD
        meld = (0.957 * math.log(cr) + 
                0.378 * math.log(bilirubin) + 
                1.120 * math.log(inr) + 
                0.643) * 10
        
        return round(meld, 1)
    
    def _create_conversion_function(
        self,
        formula_id: str,
        spec: Dict[str, Any],
        validation_schema: BaseModel,
        inputs: List[Dict[str, Any]]
    ) -> Callable:
        """Create conversion calculator function (Pint-backed; see tools/clinical_unit_conversion.py)."""

        from tool_medical_formulas.engine.clinical_unit_conversion import (
            clinical_convert,
            infer_conversion_from_inputs,
        )

        formula_section = spec.get('formula', {}) or {}
        conv = spec.get('conversion') or formula_section.get('conversion')
        if not isinstance(conv, dict):
            conv = infer_conversion_from_inputs(inputs) or {}
        if not conv or not conv.get("from_unit") or not conv.get("to_unit"):
            raise FormulaSpecError(
                f"Formula '{formula_id}': conversion calculators require a `conversion` object "
                "with `from_unit`, `to_unit`, and `value_input`, or exactly one input with "
                "distinct `unit` and `canonical_unit`."
            )
        value_input = conv.get("value_input")
        if not value_input:
            raise FormulaSpecError(
                f"Formula '{formula_id}': conversion spec must include `value_input` "
                "(input field id holding the magnitude in `from_unit`)."
            )
        from_u = str(conv["from_unit"]).strip()
        to_u = str(conv["to_unit"]).strip()

        def compute(inputs_dict: Dict[str, Any]) -> Dict[str, Any]:
            try:
                validated_inputs = validation_schema(**inputs_dict)
            except ValidationError as e:
                raise ValueError(f"Input validation failed: {e}")

            data = validated_inputs.model_dump()
            if value_input not in data:
                raise ValueError(f"Missing input field '{value_input}' for conversion")
            raw = data[value_input]
            try:
                magnitude = float(raw)
            except (TypeError, ValueError) as e:
                raise ValueError(
                    f"Conversion input '{value_input}' must be numeric; got {raw!r}"
                ) from e

            try:
                converted = clinical_convert(magnitude, from_u, to_u)
            except ValueError as e:
                raise ValueError(str(e)) from e

            out: Dict[str, Any] = {
                "result": converted,
                "formula_id": formula_id,
                "formula_name": spec.get("name", formula_id),
                "from_unit": from_u,
                "to_unit": to_u,
                "value_input": value_input,
                "input_magnitude": magnitude,
            }
            cw = FormulaCompiler._clinical_plausibility_warnings(inputs, data, formula_id)
            if cw:
                out["clinical_warnings"] = cw
            return out

        return compute
    
    def _get_interpretation(self, spec: Dict[str, Any], score: float) -> Optional[str]:
        """Extract interpretation/risk level from formula spec based on score."""
        # Look for interpretation tables or rules in the spec
        formula_section = spec.get('formula', {})
        formula_text = formula_section.get('formulaText', '')
        
        # Try to extract interpretation from formula text
        # This is a simplified version - in production, would parse more carefully
        if 'low' in formula_text.lower() and score <= 0:
            return "Low/unlikely risk"
        elif 'moderate' in formula_text.lower() and 1 <= score <= 2:
            return "Moderate risk"
        elif 'high' in formula_text.lower() and score >= 3:
            return "High/likely risk"
        
        return None
    
    @staticmethod
    def _canonicalize_input_magnitudes(
        inputs: List[Dict[str, Any]],
        input_values: Dict[str, Any],
        formula_id: str,
    ) -> Dict[str, Any]:
        """Convert numeric inputs to ``canonical_unit`` when ``unit`` differs (R17)."""
        from tool_medical_formulas.engine.clinical_unit_conversion import (
            clinical_convert,
            clinical_convert_concentration,
        )

        out = dict(input_values)
        for inp in inputs or []:
            iid = inp.get("id")
            if not iid or iid not in out:
                continue
            u = str(inp.get("unit") or "").strip()
            cu = str(inp.get("canonical_unit") or "").strip()
            if not u or not cu or u.lower() == cu.lower():
                continue
            raw = out[iid]
            if isinstance(raw, bool):
                continue
            if isinstance(raw, (int, float)):
                magnitude = float(raw)
            elif isinstance(raw, str):
                try:
                    magnitude = float(raw.strip())
                except ValueError:
                    continue
            else:
                continue
            substance = inp.get("substance")
            try:
                if substance:
                    out[iid] = clinical_convert_concentration(
                        magnitude, u, cu, str(substance)
                    )
                else:
                    out[iid] = clinical_convert(magnitude, u, cu)
            except ValueError:
                try:
                    from tool_medical_formulas.ops_metrics import incr_counter

                    incr_counter("unit_canonicalization_failed_total")
                except Exception:
                    pass
                logger.warning(
                    "Could not canonicalize %s for formula %s (%s → %s): keeping raw value",
                    iid,
                    formula_id,
                    u,
                    cu,
                )
        return out

    def get_compiled_formula(self, formula_id: str, version: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Get compiled formula from cache."""
        cache_key = f"{formula_id}:{version or 'latest'}"
        return self._compiled_cache.get(cache_key)
    
    def clear_cache(self):
        """Clear compilation cache."""
        self._compiled_cache.clear()
        self._spec_cache.clear()
        self._validation_schemas.clear()
        self._computation_functions.clear()

