class AlreadyClaimedTodayError(Exception):
    def __init__(self, user_id: int):
        self.user_id = user_id
        super().__init__(f"user {user_id} already claimed today")
