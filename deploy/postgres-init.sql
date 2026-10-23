-- One Postgres, two databases: the core's and BioBridge's. Services never
-- share tables; they talk over HTTP with signed tokens.
CREATE DATABASE biobridge OWNER odoobridger;
