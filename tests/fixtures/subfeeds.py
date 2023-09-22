SUBFEED_CONFIG = {
    "subfeed_id": "subfeed_example",
    "type": "subfeed",
    "method_name": "ads",
}

SUBFEED_CONFIG_RAISE_ERROR = {
    "subfeed_id": "subfeed_example",
    "type": "subfeed",
    "method_name": "error",
}

SUBFEED_CONFIG_NO_RAISE_ERROR = {
    "subfeed_id": "subfeed_example",
    "type": "subfeed",
    "method_name": "error",
    "raise_error": False,
}

SUBFEED_WITH_PARAMS_CONFIG = {
    "subfeed_id": "subfeed_with_params_example",
    "type": "subfeed",
    "method_name": "ads",
    "subfeed_params": {
        "limit_to_return": 10,
    },
}
