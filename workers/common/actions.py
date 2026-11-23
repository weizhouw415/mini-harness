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
