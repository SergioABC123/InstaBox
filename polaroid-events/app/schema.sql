CREATE TABLE IF NOT EXISTS events (
    event_id UUID PRIMARY KEY,
    client_name VARCHAR(120) NOT NULL,
    event_type VARCHAR(80) NOT NULL,
    event_date DATE NOT NULL,
    status VARCHAR(12) NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'finished')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS photos (
    photo_id UUID PRIMARY KEY,
    event_id UUID NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    message VARCHAR(300) NOT NULL,
    picture_key TEXT NOT NULL UNIQUE,
    polaroid_key TEXT NOT NULL UNIQUE,
    original_deleted BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS photos_event_id_idx ON photos(event_id);
