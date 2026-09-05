"""Mutate thinking effort without discarding other Anthropic output settings."""


def set_output_effort(body: dict, effort) -> None:
    output = dict(body.get('output_config') or {})
    if effort:
        output['effort'] = effort
    else:
        output.pop('effort', None)
    if output:
        body['output_config'] = output
    else:
        body.pop('output_config', None)
