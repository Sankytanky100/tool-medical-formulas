"""
Deterministic Calculator Tool - Executes compiled formula calculations.

Hard Rule: "LLM chooses, code computes"
- LLM identifies formula_id and asks for missing inputs
- Deterministic engine validates and computes exactly
"""

from typing import Dict, Any, Optional, List
import logging
from pydantic import ValidationError

from tool_medical_formulas.engine.formula_compiler import FormulaCompiler, FormulaSpecError

logger = logging.getLogger(__name__)


def convert_clinical_units(value: float, from_unit: str, to_unit: str) -> float:
    """Scalar clinical unit conversion (Pint). Delegates to ``clinical_unit_conversion``."""
    from tool_medical_formulas.engine.clinical_unit_conversion import clinical_convert

    return clinical_convert(value, from_unit, to_unit)


class CalculationError(Exception):
    """Raised when calculation fails."""
    pass


class DeterministicCalculator:
    """Deterministic calculator for medical formulas."""
    
    def __init__(self, formulas_dir: Optional[str] = None, formula_service: Optional[Any] = None):
        """
        Initialize Deterministic Calculator.
        
        Args:
            formulas_dir: Directory containing formula JSON files (fallback)
            formula_service: Optional FormulaService for database access
        """
        self.formula_service = formula_service
        self.compiler = FormulaCompiler(formulas_dir=formulas_dir, formula_service=formula_service)
        self._listed_ids: Optional[List[str]] = None
        logger.info("DeterministicCalculator initialized")
    
    def calculate(
        self,
        formula_id: str,
        inputs: Dict[str, Any],
        validate_only: bool = False
    ) -> Dict[str, Any]:
        """
        Calculate formula result from inputs.
        
        Args:
            formula_id: Formula identifier
            inputs: Input values dictionary
            validate_only: If True, only validate inputs without calculating
        
        Returns:
            Calculation result dictionary with:
            - result: Calculation output
            - validated_inputs: Validated input values
            - metadata: Formula and calculation metadata
        
        Raises:
            CalculationError: If calculation fails
        """
        from tool_medical_formulas.engine.ckd_epi_creatinine import (
            is_ckd_epi_formula_id,
            try_calculate_ckd_epi_from_inputs,
        )

        if is_ckd_epi_formula_id(formula_id):
            payload = {k: v for k, v in (inputs or {}).items() if k != "formula_id"}
            out = try_calculate_ckd_epi_from_inputs(payload)
            if validate_only:
                if out.get("status") == "success":
                    return {
                        "valid": True,
                        "validated_inputs": out.get("validated_inputs") or {},
                        "formula_id": out.get("formula_id") or formula_id,
                        "formula_name": out.get("formula_name"),
                        "message": "Inputs are valid",
                    }
                return {
                    "valid": False,
                    "missing_fields": out.get("missing_fields") or [],
                    "error": out.get("error") or "validation_failed",
                    "formula_id": out.get("formula_id") or formula_id,
                }
            if out.get("status") != "success":
                raise CalculationError(
                    out.get("error") or f"CKD-EPI validation failed for '{formula_id}'"
                )
            return out

        try:
            # Compile formula (will use cache if already compiled)
            compiled = self.compiler.compile_formula(formula_id)
            
            validation_schema = compiled['validation_schema']
            compute_function = compiled['compute_function']
            metadata = compiled['metadata']
            
            # Normalize inputs: convert integers to strings if schema expects strings, and handle yes/no -> 0/1
            normalized_inputs = {}
            for key, value in inputs.items():
                # Check if this field expects a string type in the schema
                if hasattr(validation_schema, 'model_fields') and key in validation_schema.model_fields:
                    field_info = validation_schema.model_fields[key]
                    field_type = field_info.annotation
                    
                    # If field expects string but we have int/float, convert to string
                    if field_type == str and isinstance(value, (int, float)):
                        normalized_inputs[key] = str(value)
                    # If field expects int/float but we have string, try to convert
                    elif field_type in (int, float) and isinstance(value, str):
                        try:
                            if field_type == int:
                                normalized_inputs[key] = int(value)
                            else:
                                normalized_inputs[key] = float(value)
                        except (ValueError, TypeError):
                            normalized_inputs[key] = value  # Keep original if conversion fails
                    else:
                        normalized_inputs[key] = value
                else:
                    normalized_inputs[key] = value
            
            # Validate inputs
            try:
                validated_inputs = validation_schema(**normalized_inputs)
            except ValidationError as e:
                error_details = []
                for error in e.errors():
                    field = error.get('loc', ['unknown'])[0]
                    msg = error.get('msg', 'validation error')
                    error_details.append(f"{field}: {msg}")
                
                raise CalculationError(
                    f"Input validation failed for formula '{formula_id}': "
                    f"{'; '.join(error_details)}"
                )
            
            if validate_only:
                return {
                    "valid": True,
                    "validated_inputs": validated_inputs.model_dump(),
                    "formula_id": formula_id,
                    "formula_name": metadata.get('formula_name'),
                    "message": "Inputs are valid"
                }
            
            # Execute calculation
            try:
                result = compute_function(validated_inputs.model_dump())
            except NotImplementedError as e:
                raise CalculationError(
                    f"Formula '{formula_id}' computation not yet implemented: {e}"
                )
            except Exception as e:
                raise CalculationError(
                    f"Calculation error for formula '{formula_id}': {str(e)}"
                )
            
            # Build response
            response = {
                "result": result,
                "validated_inputs": validated_inputs.model_dump(),
                "formula_id": formula_id,
                "formula_name": metadata.get('formula_name'),
                "formula_version": metadata.get('version'),
                "calculation_type": metadata.get('calculation_type'),
                "metadata": metadata
            }
            
            logger.info(
                f"Successfully calculated formula '{formula_id}' "
                f"(type: {metadata.get('calculation_type')})"
            )
            
            return response
        
        except FormulaSpecError as e:
            raise CalculationError(f"Formula spec error: {e}")
        except Exception as e:
            logger.error(f"Unexpected error calculating '{formula_id}': {e}", exc_info=True)
            raise CalculationError(f"Unexpected calculation error: {e}")
    
    def validate_inputs(
        self,
        formula_id: str,
        inputs: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Validate inputs for a formula without calculating.
        
        Returns a graceful result indicating which inputs are missing,
        rather than raising errors.
        
        Args:
            formula_id: Formula identifier
            inputs: Input values dictionary
        
        Returns:
            Validation result dictionary with:
            - valid: bool - True if all required inputs are present
            - missing_fields: List[str] - List of missing required fields
            - provided_fields: List[str] - List of provided fields
            - error: str - Error message if validation failed
        """
        from tool_medical_formulas.engine.ckd_epi_creatinine import (
            is_ckd_epi_formula_id,
            try_calculate_ckd_epi_from_inputs,
        )

        if is_ckd_epi_formula_id(formula_id):
            payload = {k: v for k, v in (inputs or {}).items() if k != "formula_id"}
            out = try_calculate_ckd_epi_from_inputs(payload)
            if out.get("status") == "success":
                return {
                    "valid": True,
                    "missing_fields": [],
                    "provided_fields": sorted(payload.keys()),
                    "formula_id": out.get("formula_id") or formula_id,
                }
            return {
                "valid": False,
                "missing_fields": out.get("missing_fields") or [],
                "provided_fields": sorted(payload.keys()),
                "error": out.get("error") or "validation_failed",
                "formula_id": out.get("formula_id") or formula_id,
            }

        try:
            # Compile formula to get validation schema
            compiled = self.compiler.compile_formula(formula_id)
            validation_schema = compiled['validation_schema']
            metadata = compiled.get('metadata', {})
            
            # Get required fields from schema
            if hasattr(validation_schema, 'model_fields'):
                required_fields = {
                    field_name: field_info
                    for field_name, field_info in validation_schema.model_fields.items()
                    if field_info.is_required()
                }
            else:
                required_fields = {}
            
            # Check which required fields are missing
            provided_fields = set(inputs.keys())
            missing_fields = [
                field_name
                for field_name in required_fields.keys()
                if field_name not in provided_fields
            ]
            
            if missing_fields:
                return {
                    "valid": False,
                    "missing_fields": missing_fields,
                    "provided_fields": list(provided_fields),
                    "required_fields": list(required_fields.keys()),
                    "formula_id": formula_id,
                    "formula_name": metadata.get('formula_name'),
                    "error": f"Missing required fields: {', '.join(missing_fields)}"
                }
            
            # All required fields present - try actual validation
            try:
                # Normalize inputs (same logic as calculate)
                normalized_inputs = {}
                for key, value in inputs.items():
                    if hasattr(validation_schema, 'model_fields') and key in validation_schema.model_fields:
                        field_info = validation_schema.model_fields[key]
                        field_type = field_info.annotation
                        
                        if field_type == str and isinstance(value, (int, float)):
                            normalized_inputs[key] = str(value)
                        elif field_type in (int, float) and isinstance(value, str):
                            try:
                                normalized_inputs[key] = field_type(value)
                            except (ValueError, TypeError):
                                normalized_inputs[key] = value
                        else:
                            normalized_inputs[key] = value
                    else:
                        normalized_inputs[key] = value
                
                # Try validation
                validated_inputs = validation_schema(**normalized_inputs)
                
                return {
                    "valid": True,
                    "validated_inputs": validated_inputs.model_dump(),
                    "provided_fields": list(provided_fields),
                    "required_fields": list(required_fields.keys()),
                    "formula_id": formula_id,
                    "formula_name": metadata.get('formula_name'),
                    "message": "All required inputs are present and valid"
                }
            except ValidationError as e:
                # Validation failed due to type/value errors
                error_details = []
                for error in e.errors():
                    field = error.get('loc', ['unknown'])[0]
                    msg = error.get('msg', 'validation error')
                    error_details.append(f"{field}: {msg}")
                
                return {
                    "valid": False,
                    "provided_fields": list(provided_fields),
                    "required_fields": list(required_fields.keys()),
                    "formula_id": formula_id,
                    "formula_name": metadata.get('formula_name'),
                    "error": f"Validation errors: {'; '.join(error_details)}"
                }
        except Exception as e:
            # If compilation or other error occurs, return graceful failure
            logger.warning(f"Could not validate inputs for {formula_id}: {e}")
            return {
                "valid": False,
                "provided_fields": list(inputs.keys()),
                "formula_id": formula_id,
                "error": f"Could not validate inputs: {str(e)}"
            }
    
    def get_formula_info(self, formula_id: str) -> Dict[str, Any]:
        """
        Get formula information and input schema.
        
        Args:
            formula_id: Formula identifier
        
        Returns:
            Formula information dictionary
        """
        # Try FormulaService first (database)
        if self.formula_service:
            metadata = self.formula_service.get_formula_metadata(formula_id)
            if metadata:
                return {
                    "formula_id": metadata.get("formula_id", formula_id),
                    "name": metadata.get("name", ""),
                    "description": metadata.get("description", ""),
                    "inputs": metadata.get("inputs", []),
                    "calculation_type": metadata.get("calculation_type", ""),
                    "version": metadata.get("version", "1.0.0"),
                    "specialty_relevance": metadata.get("specialty_relevance", [])
                }
        
        # Fallback to loading from file
        try:
            spec = self.compiler.load_formula(formula_id)
            
            # Compile to get validation schema
            compiled = self.compiler.compile_formula(formula_id, spec)
            
            # Extract input information
            inputs_info = []
            for input_def in spec.get('inputs', []):
                input_info = {
                    "id": input_def.get('id'),
                    "label": input_def.get('label'),
                    "description": input_def.get('description'),
                    "type": input_def.get('type'),
                    "required": input_def.get('required', True),
                    "options": input_def.get('options', [])
                }
                inputs_info.append(input_info)
            
            return {
                "formula_id": formula_id,
                "name": spec.get('name', formula_id),
                "description": spec.get('description', ''),
                "inputs": inputs_info,
                "calculation_type": compiled['metadata'].get('calculation_type'),
                "version": spec.get('version', '1.0.0')
            }
        
        except FormulaSpecError as e:
            raise CalculationError(f"Formula not found: {e}")
    
    def list_available_formulas(self) -> List[str]:
        """
        List all available formula IDs.
        
        Returns:
            List of formula IDs
        """
        if self._listed_ids is not None:
            return list(self._listed_ids)
        # Scan formulas directory once per calculator instance.
        formula_files = list(self.compiler.formulas_dir.glob("*.json"))
        formula_ids = []
        
        for file_path in formula_files:
            # Extract formula ID from filename
            filename = file_path.stem
            # Remove prefixes like "merged_" or "unmatched2_"
            formula_id = filename.replace("merged_", "").replace("unmatched2_", "")
            formula_ids.append(formula_id)
        
        self._listed_ids = sorted(formula_ids)
        return list(self._listed_ids)

