"""
Formula Parser - Generic formula parser for extracting structured calculation logic.

This module provides generic parsing capabilities that work with any formula type
without hardcoding specific formula implementations.
"""

from typing import Dict, Any, List, Optional
from dataclasses import dataclass
import re
import logging

logger = logging.getLogger(__name__)


@dataclass
class FormulaStep:
    """Represents a single calculation step in a formula."""
    variable: str
    expression: str
    description: Optional[str] = None


@dataclass
class FormulaConstraint:
    """Represents a constraint on a formula variable."""
    variable: str
    condition: str  # e.g., "> 0", ">= 1.0"
    error_message: str


@dataclass
class FormulaConditional:
    """Represents conditional logic (if-then rules)."""
    condition: str  # e.g., "dialysis == '1'"
    actions: List[Dict[str, Any]]  # e.g., [{"variable": "creatinine", "value": 4.0}]


@dataclass
class FormulaMinMax:
    """Represents min/max constraints on variables."""
    variable: str
    min: Optional[float] = None
    max: Optional[float] = None


@dataclass
class FormulaSpec:
    """Structured representation of formula logic."""
    calculation_type: str  # "points_sum", "mathematical", "conversion"
    steps: List[FormulaStep]
    variable_mappings: Dict[str, str]  # input_field_id -> formula_variable_name
    constraints: List[FormulaConstraint]
    conditionals: List[FormulaConditional]
    min_max: List[FormulaMinMax]
    formula_id: str
    formula_name: str


class FormulaParser:
    """
    Generic formula parser that extracts structured calculation logic from formula JSON specs.
    
    This parser works with any formula type without hardcoding specific implementations.
    """
    
    def __init__(self):
        """Initialize Formula Parser."""
        self.logger = logging.getLogger(__name__)
    
    def parse_formula_spec(self, spec: Dict[str, Any]) -> FormulaSpec:
        """
        Parse formula JSON spec into structured FormulaSpec.
        
        Args:
            spec: Formula JSON dictionary from database
        
        Returns:
            Structured FormulaSpec object
        """
        formula_id = spec.get('id') or spec.get('formula_id', 'unknown')
        formula_name = spec.get('name', formula_id)
        
        # Determine calculation type
        calculation_type = self._determine_calculation_type(spec)
        
        # Extract calculation steps
        steps = self._extract_steps(spec, calculation_type)
        
        # Build variable mappings
        # NOTE: Variable extraction is now handled by AI agent (FormulaAnalysisAgent)
        # This method now only uses inputs array for backward compatibility
        inputs = spec.get('inputs', [])
        formula_variables = [inp.get('id') for inp in inputs if inp.get('id')]
        variable_mappings = self.build_variable_mapping(inputs, formula_variables)
        
        # Extract constraints
        constraints = self._extract_constraints(spec)
        
        # Extract conditionals
        conditionals = self._extract_conditionals(spec)
        
        # Extract min/max constraints
        min_max = self._extract_min_max(spec)
        
        return FormulaSpec(
            calculation_type=calculation_type,
            steps=steps,
            variable_mappings=variable_mappings,
            constraints=constraints,
            conditionals=conditionals,
            min_max=min_max,
            formula_id=formula_id,
            formula_name=formula_name
        )
    
    def _determine_calculation_type(self, spec: Dict[str, Any]) -> str:
        """
        Determine calculation type from formula spec.
        
        Returns:
            'points_sum', 'mathematical', or 'conversion'
        """
        # Check if it's a points-based scoring system
        inputs = spec.get('inputs', [])
        has_points = any(
            any(opt.get('points') is not None for opt in input_def.get('options', []))
            for input_def in inputs
        )
        
        if has_points:
            return 'points_sum'
        
        # Check for mathematical expressions
        formula_section = spec.get('formula', {})
        if formula_section.get('latexFormulas') or formula_section.get('formulaText'):
            formula_text = str(formula_section.get('formulaText', ''))
            raw_text = str(formula_section.get('rawText', ''))
            combined_text = formula_text + ' ' + raw_text
            
            # Strong indicators of mathematical formula
            if any(indicator in combined_text.lower() for indicator in ['ln(', 'log(', 'exp(', 'e^', 'math', 'formula']):
                return 'mathematical'
            
            # Check for mathematical operations
            if any(op in combined_text for op in ['*', '/', '^', '**', 'ln(', 'log(']):
                return 'mathematical'
        
        # Check for conversion patterns
        if any(keyword in str(spec).lower() for keyword in ['dosing', 'conversion', 'dose', 'units']):
            return 'conversion'
        
        # Default to points_sum
        return 'points_sum'
    
    def _extract_steps(self, spec: Dict[str, Any], calculation_type: str) -> List[FormulaStep]:
        """
        Extract calculation steps from formula spec.
        
        Args:
            spec: Formula JSON dictionary
            calculation_type: Type of calculation
        
        Returns:
            List of FormulaStep objects
        """
        steps = []
        formula_section = spec.get('formula', {})
        
        if calculation_type == 'points_sum':
            # Points-based: steps are implicit (sum all points)
            steps.append(FormulaStep(
                variable="result",
                expression="sum(points)",
                description="Sum of all points from selected options"
            ))
        
        elif calculation_type == 'mathematical':
            # Extract from rawText or formulaText
            raw_text = formula_section.get('rawText', '')
            formula_text = formula_section.get('formulaText', '')
            
            # Try to extract multi-step formulas
            steps = self._parse_mathematical_steps(raw_text or formula_text, spec)
        
        elif calculation_type == 'conversion':
            # Conversion formulas
            raw_text = formula_section.get('rawText', '')
            steps = self._parse_conversion_steps(raw_text, spec)
        
        return steps
    
    def _parse_mathematical_steps(self, formula_text: str, spec: Dict[str, Any]) -> List[FormulaStep]:
        """
        Parse mathematical formula text into steps.
        
        Handles patterns like:
        - Single expression: "result = expression"
        - Multi-step: "y = ...", then "result = f(y)"
        """
        steps = []
        
        if not formula_text:
            return steps
        
        # Look for patterns like "y = ..." or "result = ..."
        # Pattern: variable = expression
        step_pattern = r'(\w+)\s*=\s*([^,;]+?)(?:[,;]|$)'
        matches = re.finditer(step_pattern, formula_text, re.IGNORECASE)
        
        intermediate_vars = []
        for match in matches:
            var_name = match.group(1).strip()
            expression = match.group(2).strip()
            
            # Skip if it's a description or explanation
            if len(expression) > 200 or 'where' in expression.lower():
                continue
            
            # Clean expression
            expression = self.normalize_expression(expression)
            
            if var_name.lower() in ['y', 'x', 'intermediate', 'temp']:
                intermediate_vars.append(var_name)
                steps.append(FormulaStep(
                    variable=var_name,
                    expression=expression,
                    description=f"Intermediate calculation: {var_name}"
                ))
            elif var_name.lower() in ['result', 'final', 'score', 'value']:
                steps.append(FormulaStep(
                    variable="result",
                    expression=expression,
                    description="Final calculation result"
                ))
        
        # If no steps found, try to extract main formula
        if not steps:
            # Look for main formula pattern
            main_patterns = [
                r'=\s*([^,;]+?)(?:[,;]|$)',
                r'Formula[:\s]+([^,;]+?)(?:[,;]|$)',
            ]
            
            for pattern in main_patterns:
                match = re.search(pattern, formula_text, re.IGNORECASE)
                if match:
                    expression = match.group(1).strip()
                    expression = self.normalize_expression(expression)
                    steps.append(FormulaStep(
                        variable="result",
                        expression=expression,
                        description="Main formula calculation"
                    ))
                    break
        
        return steps
    
    def _parse_conversion_steps(self, formula_text: str, spec: Dict[str, Any]) -> List[FormulaStep]:
        """Parse conversion formula text into steps."""
        steps = []
        
        if not formula_text:
            return steps
        
        # Similar to mathematical parsing
        steps = self._parse_mathematical_steps(formula_text, spec)
        
        return steps
    
    def extract_variables(self, expression: str) -> List[str]:
        """
        Extract variable names from an expression.
        
        DEPRECATED: Variable extraction is now handled by AI agent (FormulaAnalysisAgent).
        This method is kept for backward compatibility only.
        
        For intelligent variable extraction, use FormulaAnalysisAgent instead.
        
        Args:
            expression: Mathematical expression string
        
        Returns:
            List of variable names found in expression
        """
        # Remove function calls and constants
        cleaned = re.sub(r'\b(ln|log|exp|sqrt|abs|max|min|round|floor|ceil|pow|sin|cos|tan)\s*\(', '', expression)
        cleaned = re.sub(r'\b(math\.)?(pi|e)\b', '', cleaned)
        cleaned = re.sub(r'\d+\.?\d*', '', cleaned)  # Remove numbers
        cleaned = re.sub(r'[+\-*/()^=,;]', ' ', cleaned)  # Remove operators
        
        # Extract words (potential variables)
        variables = []
        for word in cleaned.split():
            word = word.strip()
            if word and word not in ['and', 'or', 'where', 'if', 'then', 'else']:
                # Check if it looks like a variable name
                if re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', word):
                    variables.append(word)
        
        return list(set(variables))  # Remove duplicates
    
    def normalize_expression(self, expression: str) -> str:
        """
        Normalize formula expression to standard format.
        
        Args:
            expression: Raw expression string
        
        Returns:
            Normalized expression
        """
        # Convert to lowercase for consistency
        normalized = expression.lower()
        
        # Replace common patterns
        normalized = re.sub(r'\bln\s*\(', 'ln(', normalized)
        normalized = re.sub(r'\blog\s*\(', 'log(', normalized)
        normalized = re.sub(r'\bexp\s*\(', 'exp(', normalized)
        normalized = re.sub(r'\bsqrt\s*\(', 'sqrt(', normalized)
        
        # Replace ^ with ** for power
        normalized = normalized.replace('^', '**')
        
        # Replace x with * for multiplication (when between numbers/variables)
        normalized = re.sub(r'(\d+)\s*x\s*(\d+|[a-zA-Z_])', r'\1 * \2', normalized)
        normalized = re.sub(r'([a-zA-Z_]+)\s*x\s*(\d+|[a-zA-Z_])', r'\1 * \2', normalized)
        
        # Clean up whitespace
        normalized = ' '.join(normalized.split())
        
        return normalized
    
    def build_variable_mapping(
        self,
        inputs: List[Dict[str, Any]],
        formula_variables: List[str]
    ) -> Dict[str, str]:
        """
        Build mapping from input field IDs to formula variable names.
        
        Uses fuzzy matching to handle variations in naming.
        
        Args:
            inputs: List of input field definitions
            formula_variables: List of variable names from formula
        
        Returns:
            Dictionary mapping input_field_id -> formula_variable_name
        """
        mapping = {}
        
        for input_def in inputs:
            input_id = input_def.get('id')
            if not input_id:
                continue
            
            # Try exact match first
            if input_id in formula_variables:
                mapping[input_id] = input_id
                continue
            
            # Try fuzzy matching
            best_match = self._fuzzy_match(input_id, formula_variables)
            if best_match:
                mapping[input_id] = best_match
                continue
            
            # Try matching by name field
            input_name = input_def.get('name', '')
            if input_name and input_name in formula_variables:
                mapping[input_id] = input_name
                continue
            
            # Try partial matching
            for var in formula_variables:
                if input_id.lower() in var.lower() or var.lower() in input_id.lower():
                    mapping[input_id] = var
                    break
        
        return mapping
    
    def _fuzzy_match(self, input_id: str, variables: List[str]) -> Optional[str]:
        """
        Fuzzy match input ID to formula variable.
        
        Handles common patterns:
        - Abbreviations: "trig" -> "triglycerides"
        - Underscores: "waist_circumference" -> "waist circumference"
        - Case variations
        """
        input_lower = input_id.lower()
        
        # Common abbreviation mappings
        abbrev_map = {
            'trig': 'triglycerides',
            'bmi': 'bmi',
            'ggt': 'ggt',
            'circ': 'waist_circumference',
            'waist': 'waist_circumference',
            'cr': 'creatinine',
            'bil': 'bilirubin',
            'inr': 'inr',
            'na': 'sodium',
            'glucose': 'glucose',
            'hco3': 'bicarbonate',
            'pco2': 'pco2',
        }
        
        # Check abbreviation map
        if input_lower in abbrev_map:
            target = abbrev_map[input_lower]
            if target in variables:
                return target
        
        # Try substring matching
        for var in variables:
            var_lower = var.lower()
            
            # Check if input is substring of variable or vice versa
            if input_lower in var_lower or var_lower in input_lower:
                # Prefer longer matches
                if len(var) > len(input_id):
                    return var
                else:
                    return var
        
        return None
    
    def _extract_formula_variables(self, spec: Dict[str, Any]) -> List[str]:
        """
        Extract variable names from formula spec.
        
        DEPRECATED: Variable extraction is now handled by AI agent (FormulaAnalysisAgent).
        This method is kept for backward compatibility and only extracts from inputs array.
        
        For intelligent variable extraction, use FormulaAnalysisAgent instead.
        """
        # Only extract from inputs array (backward compatibility)
        variables = []
        for input_def in spec.get('inputs', []):
            input_id = input_def.get('id')
            if input_id:
                variables.append(input_id)
        
        return list(set(variables))  # Remove duplicates
    
    def _extract_constraints(self, spec: Dict[str, Any]) -> List[FormulaConstraint]:
        """Extract constraints from formula spec."""
        constraints = []
        formula_section = spec.get('formula', {})
        formula_text = str(formula_section.get('formulaText', ''))
        raw_text = str(formula_section.get('rawText', ''))
        combined_text = formula_text + ' ' + raw_text
        
        # Look for constraint patterns in text
        # Pattern: "must be > 0", "should be >= 1", etc.
        constraint_patterns = [
            r'(\w+)\s+(?:must|should|needs?)\s+be\s+([><=]+)\s*(\d+\.?\d*)',
            r'(\w+)\s+([><=]+)\s*(\d+\.?\d*)',
        ]
        
        for pattern in constraint_patterns:
            matches = re.finditer(pattern, combined_text, re.IGNORECASE)
            for match in matches:
                var_name = match.group(1).strip()
                operator = match.group(2).strip()
                value = match.group(3).strip()
                
                constraints.append(FormulaConstraint(
                    variable=var_name,
                    condition=f"{operator} {value}",
                    error_message=f"{var_name} must be {operator} {value}"
                ))
        
        return constraints
    
    def _extract_conditionals(self, spec: Dict[str, Any]) -> List[FormulaConditional]:
        """Extract conditional logic from formula spec."""
        conditionals = []
        formula_section = spec.get('formula', {})
        formula_text = str(formula_section.get('formulaText', ''))
        raw_text = str(formula_section.get('rawText', ''))
        combined_text = formula_text + ' ' + raw_text
        
        # Look for conditional patterns
        # Pattern: "if dialysis then creatinine = 4.0"
        conditional_patterns = [
            r'if\s+(\w+)\s*(?:==|is|equals?)\s*([^\s,;]+)\s+then\s+(\w+)\s*=\s*(\d+\.?\d*)',
            r'(\w+)\s*(?:==|is|equals?)\s*([^\s,;]+)\s+.*?(\w+)\s*=\s*(\d+\.?\d*)',
        ]
        
        for pattern in conditional_patterns:
            matches = re.finditer(pattern, combined_text, re.IGNORECASE)
            for match in matches:
                condition_var = match.group(1).strip()
                condition_val = match.group(2).strip()
                action_var = match.group(3).strip()
                action_val = match.group(4).strip()
                
                try:
                    action_value = float(action_val)
                except ValueError:
                    continue
                
                conditionals.append(FormulaConditional(
                    condition=f"{condition_var} == '{condition_val}'",
                    actions=[{"variable": action_var, "value": action_value}]
                ))
        
        return conditionals
    
    def _extract_min_max(self, spec: Dict[str, Any]) -> List[FormulaMinMax]:
        """Extract min/max constraints from formula spec."""
        min_max_list = []
        formula_section = spec.get('formula', {})
        formula_text = str(formula_section.get('formulaText', ''))
        raw_text = str(formula_section.get('rawText', ''))
        combined_text = formula_text + ' ' + raw_text
        
        # Look for min/max patterns
        # Pattern: "minimum 1.0", "max 4.0", ">= 1.0", etc.
        min_pattern = r'(\w+)\s+(?:minimum|min|>=)\s*(\d+\.?\d*)'
        max_pattern = r'(\w+)\s+(?:maximum|max|<=)\s*(\d+\.?\d*)'
        
        for match in re.finditer(min_pattern, combined_text, re.IGNORECASE):
            var_name = match.group(1).strip()
            min_val = float(match.group(2).strip())
            min_max_list.append(FormulaMinMax(variable=var_name, min=min_val))
        
        for match in re.finditer(max_pattern, combined_text, re.IGNORECASE):
            var_name = match.group(1).strip()
            max_val = float(match.group(2).strip())
            # Update existing entry or create new
            existing = next((m for m in min_max_list if m.variable == var_name), None)
            if existing:
                existing.max = max_val
            else:
                min_max_list.append(FormulaMinMax(variable=var_name, max=max_val))
        
        return min_max_list

