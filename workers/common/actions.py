GUI_ACTIONS = ("screenshot", "click", "double_click", "right_click", "move", "drag", "type", "key", "scroll")
SCROLL_DIRECTIONS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}


def text_field(action: dict, name: str, *, allow_empty: bool = False, limit: int = 8192) -> str:
    value = action.get(name)
    if not isinstance(value, str) or (not allow_empty and not value.strip()) or len(value) > limit:
        raise ValueError(f"{name} must be a string of at most {limit} characters")
    return value


def integer_field(action: dict, name: str, *, default: int | None = None) -> int:
    value = action.get(name, default)
    if type(value) is not int or abs(value) > 100_000:
        raise ValueError(f"{name} must be an integer between -100000 and 100000")
    return value


def point_fields(action: dict, x_name: str = "x", y_name: str = "y") -> tuple[int, int]:
    return integer_field(action, x_name), integer_field(action, y_name)


def scroll_deltas(action: dict) -> tuple[int, int]:
    if "direction" not in action and "amount" not in action:
        return integer_field(action, "dx", default=0), integer_field(action, "dy", default=0)
    if "dx" in action or "dy" in action:
        raise ValueError("Scroll direction and amount cannot be combined with dx or dy")
    direction = action.get("direction")
    if not isinstance(direction, str) or direction not in SCROLL_DIRECTIONS:
        raise ValueError("Scroll direction must be up, down, left, or right")
    amount = integer_field(action, "amount")
    if amount < 1:
        raise ValueError("Scroll amount must be positive")
    x_sign, y_sign = SCROLL_DIRECTIONS[direction]
    return x_sign * amount, y_sign * amount
