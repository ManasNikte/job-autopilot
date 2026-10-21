"""Small formatting helpers shared by the app."""


def strf(dt, fmt: str) -> str:
    """strftime with %-d / %-I (no leading zero) on every OS; Windows' strftime rejects the '-' flag."""
    return dt.strftime(fmt.replace("%-d", str(dt.day)).replace("%-I", str(int(dt.strftime("%I")))))
