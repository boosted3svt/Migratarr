"""Conservative temporary headroom for atomic, per-file replacement."""


def temporary_budget(available_bytes, next_file_bytes, recovery_headroom_bytes):
    for value in (available_bytes, next_file_bytes, recovery_headroom_bytes):
        if type(value) is not int or value < 0:
            raise ValueError("temporary-space values must be nonnegative integer bytes")
    required = next_file_bytes + recovery_headroom_bytes
    return {"status": "sufficient" if available_bytes >= required else "insufficient",
            "available_bytes": available_bytes, "next_file_bytes": next_file_bytes,
            "protected_headroom_bytes": recovery_headroom_bytes, "required_bytes": required,
            "whole_tree_staging": False, "existing_target_bytes_credited": 0,
            "on_insufficient_space": "block_until_space_is_freed_by_a_reviewed_action"}
