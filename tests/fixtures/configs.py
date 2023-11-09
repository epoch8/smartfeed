from typing import Callable, Dict

from smartfeed.examples.example_client import ClientMixerClass

METHODS_DICT: Dict[str, Callable] = {
    "ads": ClientMixerClass().example_method,
    "followings": ClientMixerClass().example_method,
    "empty": ClientMixerClass().empty_method,
    "error": ClientMixerClass().error_method,
    "doubles": ClientMixerClass().doubles_method,
}

PARSING_CONFIG_FIXTURE = {
    "version": "1",
    "feed": {
        "merger_id": "merger_positional_parsing_example",
        "type": "merger_positional",
        "positions": [1, 3, 15],
        "start": 17,
        "end": 200,
        "step": 2,
        "positional": {
            "merger_id": "merger_append_parsing_example",
            "type": "merger_append",
            "items": [
                {
                    "subfeed_id": "subfeed_merger_append_parsing_example",
                    "type": "subfeed",
                    "method_name": "ads",
                    "subfeed_params": {
                        "limit_to_return": 10,
                    },
                },
                {
                    "merger_id": "merger_percentage_gradient_parsing_example",
                    "type": "merger_percentage_gradient",
                    "item_from": {
                        "percentage": 80,
                        "data": {
                            "subfeed_id": "subfeed_from_merger_percentage_gradient_parsing_example",
                            "type": "subfeed",
                            "method_name": "followings",
                        },
                    },
                    "item_to": {
                        "percentage": 20,
                        "data": {
                            "subfeed_id": "subfeed_to_merger_percentage_gradient_parsing_example",
                            "type": "subfeed",
                            "method_name": "followings",
                        },
                    },
                    "step": 10,
                    "size_to_step": 30,
                    "shuffle": False,
                },
                {
                    "merger_id": "merger_view_session_parsing_example",
                    "type": "merger_view_session",
                    "session_size": 800,
                    "session_live_time": 300,
                    "data": {
                        "subfeed_id": "subfeed_merger_view_session_parsing_example",
                        "type": "subfeed",
                        "method_name": "followings",
                    },
                },
            ],
        },
        "default": {
            "merger_id": "merger_percentage_parsing_example",
            "type": "merger_percentage",
            "shuffle": False,
            "items": [
                {
                    "percentage": 100,
                    "data": {
                        "subfeed_id": "subfeed_merger_percentage_parsing_example",
                        "type": "subfeed",
                        "method_name": "followings",
                        "raise_error": False,
                    },
                },
            ],
        },
    },
}
