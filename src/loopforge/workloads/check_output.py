"""Pure completion checks shared by authoritative workload consumers."""


def completion_marker_present(stdout: str, marker: str) -> bool:
    """Only the exact final output line completes a frozen probe."""
    return bool(marker) and stdout.splitlines()[-1:] == [marker]
