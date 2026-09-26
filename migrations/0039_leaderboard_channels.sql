-- Persistent leaderboard boards: one bot-owned message per bound channel,
-- edited in place by the bot's leaderboard loop.
CREATE TABLE leaderboard_channels (
    guild_id BIGINT PRIMARY KEY,
    channel_id BIGINT NOT NULL,
    message_id BIGINT,          -- NULL until the loop posts the board
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Seed: the Wall Street server's #highest-net-worth channel.
INSERT INTO leaderboard_channels (guild_id, channel_id)
VALUES (1550445860330934382, 1553235177545932820);
