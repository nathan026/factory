\getenv factory_password FACTORY_DB_PASSWORD
\getenv scheduler_password HATCHET_DB_PASSWORD
CREATE ROLE factory LOGIN PASSWORD :'factory_password';
CREATE ROLE hatchet LOGIN PASSWORD :'scheduler_password';
CREATE DATABASE factory OWNER factory;
CREATE DATABASE hatchet OWNER hatchet;
REVOKE CONNECT ON DATABASE factory FROM PUBLIC;
REVOKE CONNECT ON DATABASE hatchet FROM PUBLIC;
