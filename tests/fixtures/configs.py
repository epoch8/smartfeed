EXAMPLE_CLIENT_FEED = {
    "version": "1",
    "view_session": False,
    "session_size": 800,
    "session_live_time": 300,
    "feed": {
        "merger_id": "merger_pos",
        "type": "merger_positional",
        "positions": [1, 3, 15],
        "start": 17,
        "end": 200,
        "step": 2,
        "positional": {
            "subfeed_id": "sf_positional",
            "type": "subfeed",
            "method_name": "ads",
        },
        "default": {
            "merger_id": "merger_percent",
            "type": "merger_percentage",
            "shuffle": False,
            "items": [
                {
                    "percentage": 40,
                    "data": {
                        "subfeed_id": "sf_1_default_merger_of_main",
                        "type": "subfeed",
                        "method_name": "followings",
                    },
                },
                {
                    "percentage": 60,
                    "data": {
                        "subfeed_id": "sf_2_default_merger_of_main",
                        "type": "subfeed",
                        "method_name": "ads",
                    },
                },
            ],
        },
    },
}
