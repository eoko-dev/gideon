"""Safe mathematical expression evaluation (calculate tool).

Expressions come from model output derived from user text, so they are
untrusted. They are parsed with ``ast`` and evaluated by walking a whitelist
of node types — nothing is ever passed to ``eval``. Sizes are bounded so a
hostile expression (e.g. ``9**9**9**9``) fails fast instead of hanging the
event loop.
"""

import ast
import logging
import math
import operator
import re

logger = logging.getLogger('calculation_handler')

MAX_EXPRESSION_LENGTH = 300
MAX_AST_NODES = 200
MAX_INT_DIGITS = 1000        # largest exact integer we will carry around
MAX_POWER_DIGITS = 400       # estimated digits a single power may produce
MAX_FACTORIAL = 170          # largest n with a finite float factorial

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}

_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _factorial(n):
    if isinstance(n, float):
        if not n.is_integer():
            raise ValueError("Factorial is only defined for whole numbers.")
        n = int(n)
    if n < 0 or n > MAX_FACTORIAL:
        raise ValueError(f"Factorial is limited to 0..{MAX_FACTORIAL}.")
    return math.factorial(n)


def _log(x, base=None):
    return math.log(x) if base is None else math.log(x, base)


_FUNCTIONS = {
    "sqrt": math.sqrt,
    "cbrt": lambda x: math.copysign(abs(x) ** (1 / 3), x),
    "abs": abs,
    "round": round,
    "floor": math.floor,
    "ceil": math.ceil,
    "exp": math.exp,
    "log": _log,
    "ln": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "sinh": math.sinh,
    "cosh": math.cosh,
    "tanh": math.tanh,
    "degrees": math.degrees,
    "radians": math.radians,
    "factorial": _factorial,
    "min": min,
    "max": max,
}

_CONSTANTS = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
}

_NAMES = set(_FUNCTIONS) | set(_CONSTANTS)


def _check_size(value):
    """Rejects integers too large to keep working with cheaply."""
    if isinstance(value, int) and not isinstance(value, bool):
        if value.bit_length() > MAX_INT_DIGITS * 3.33:
            raise ValueError("The result is too large to compute.")
    elif isinstance(value, float) and (math.isinf(value) or math.isnan(value)):
        raise ValueError("The result is too large or undefined.")
    return value


def _safe_pow(base, exponent):
    if isinstance(exponent, (int, float)) and abs(exponent) > 10_000:
        raise ValueError("The exponent is too large.")
    if base not in (0, 1, -1) and exponent > 0:
        estimated_digits = exponent * math.log10(abs(base)) if abs(base) > 0 else 0
        if estimated_digits > MAX_POWER_DIGITS:
            raise ValueError("The result is too large to compute.")
    result = operator.pow(base, exponent)
    if isinstance(result, complex):
        raise ValueError("The result is not a real number.")
    return result


def _evaluate(node):
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("Only numbers are allowed.")
        return _check_size(node.value)
    if isinstance(node, ast.BinOp):
        left = _evaluate(node.left)
        right = _evaluate(node.right)
        if isinstance(node.op, ast.Pow):
            return _check_size(_safe_pow(left, right))
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ValueError("Unsupported operator.")
        try:
            return _check_size(op(left, right))
        except ZeroDivisionError:
            raise ValueError("Division by zero.")
    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise ValueError("Unsupported operator.")
        return op(_evaluate(node.operand))
    if isinstance(node, ast.Name):
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise ValueError(f"Unknown name '{node.id}'.")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            raise ValueError("Unsupported function.")
        if node.keywords:
            raise ValueError("Keyword arguments are not supported.")
        args = [_evaluate(a) for a in node.args]
        try:
            return _check_size(_FUNCTIONS[node.func.id](*args))
        except (TypeError, OverflowError) as e:
            raise ValueError(f"Invalid use of {node.func.id}: {e}")
    raise ValueError("Unsupported syntax in expression.")


_IMPLICIT_MULT = [
    # 2pi, 2sqrt(4), 2(3+4)  → 2*pi, 2*sqrt(4), 2*(3+4)
    (re.compile(r"(\d|\.)\s*(?=[a-z(])"), r"\1*"),
    # (2)(3), (2)4, (2)pi  → (2)*(3), ...
    (re.compile(r"\)\s*(?=[\w(])"), r")*"),
    # pi(2), e2 are left alone: names followed by "(" are function calls
]


def _normalize(expr_str: str) -> str:
    expr_str = expr_str.replace("^", "**").replace("×", "*").replace("÷", "/")
    expr_str = expr_str.replace("−", "-")
    # Thousands separators: 1,000,000 → 1000000 (but keep max(1, 2))
    expr_str = re.sub(r"(?<=\d),(?=\d{3}\b)", "", expr_str)
    # Postfix factorial: 5! → factorial(5)
    expr_str = re.sub(r"(\d+(?:\.\d+)?)!", r"factorial(\1)", expr_str)
    for pattern, repl in _IMPLICIT_MULT:
        expr_str = pattern.sub(repl, expr_str)
    # Undo implicit multiplication inserted inside scientific notation (1e5)
    expr_str = re.sub(r"(\d)\*e(?=[+-]?\d)", r"\1e", expr_str)
    # Undo "*" inserted between a digit-suffixed function name and "(" (log10(…))
    for name in sorted(_FUNCTIONS, key=len, reverse=True):
        if any(ch.isdigit() for ch in name):
            broken = re.sub(r"(\d)$", r"\1*", name)
            expr_str = expr_str.replace(broken + "(", name + "(")
    return expr_str


def _rewrite_words(expr_str: str) -> str:
    """Applies the natural-language rewrites the tool has always accepted."""
    lowered = expr_str.lower()

    if '%' in lowered and 'of' in lowered:
        # "15% of 250" → "(15/100) * 250"
        parts = lowered.split('of')
        if len(parts) == 2:
            percent_part = parts[0].replace('%', '').strip()
            number_part = parts[1].strip()
            return f"({percent_part}/100) * {number_part}"
    elif '%' in lowered:
        lowered = lowered.replace('%', '/100')

    lowered = lowered.replace('squared', '**2').replace('cubed', '**3')
    lowered = lowered.replace('square root of', 'sqrt').replace('sqrt of', 'sqrt')
    # "sqrt 16" → "sqrt(16)"
    lowered = re.sub(r"\bsqrt\s+(\d+(?:\.\d+)?)", r"sqrt(\1)", lowered)
    return lowered


def evaluate_expression(expression: str) -> str:
    """Evaluates a natural-language math expression and returns the result.

    Raises ValueError with a user-facing explanation on failure.
    """
    if not expression or not expression.strip():
        raise ValueError("No mathematical expression provided.")
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise ValueError("The expression is too long.")

    expr_str = _normalize(_rewrite_words(expression.strip()))

    try:
        tree = ast.parse(expr_str, mode="eval")
    except SyntaxError as e:
        logger.warning(f"[Calculation] Failed to parse '{expression}': {e}")
        raise ValueError("The mathematical expression seems malformed.") from e

    if sum(1 for _ in ast.walk(tree)) > MAX_AST_NODES:
        raise ValueError("The expression is too complex.")

    try:
        result = _evaluate(tree)
        result_float = float(result)
    except ValueError:
        raise
    except (OverflowError, ArithmeticError) as e:
        raise ValueError("The result is too large or undefined.") from e
    except Exception as e:
        logger.warning(f"[Calculation] Failed to evaluate '{expression}': {e}")
        raise ValueError(f"Could not evaluate the expression: {e}") from e

    if isinstance(result, int):
        formatted_result = str(result) if abs(result) < 10**21 else f"{result_float:.15g}"
    elif abs(result_float) < 1e-10:
        formatted_result = "0"
    elif result_float == int(result_float) and abs(result_float) < 1e15:
        formatted_result = str(int(result_float))
    elif abs(result_float) >= 1e15:
        formatted_result = f"{result_float:.15g}"
    else:
        formatted_result = f"{result_float:.10f}".rstrip('0').rstrip('.')

    return f"{expression} = {formatted_result}"
