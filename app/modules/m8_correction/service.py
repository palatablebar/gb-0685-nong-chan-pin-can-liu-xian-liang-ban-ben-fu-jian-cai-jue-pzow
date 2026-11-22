from app.modules.m8_correction.rules import replay


def execute(base_state, target_id, replacement, parameters=None):
    return replay(base_state, target_id, replacement, parameters)
