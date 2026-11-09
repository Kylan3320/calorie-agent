-- Calorie Agent V3 MySQL schema.
-- This schema is not activated by default; the legacy Bitable path remains the
-- online write path until the integration thread switches it explicitly.

CREATE TABLE IF NOT EXISTS tenants (
  tenant_id VARCHAR(80) PRIMARY KEY,
  feishu_app_id VARCHAR(128) NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'active',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_tenants_feishu_app_id (feishu_app_id),
  KEY idx_tenants_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS users (
  user_id VARCHAR(80) PRIMARY KEY,
  tenant_id VARCHAR(80) NOT NULL,
  feishu_open_id VARCHAR(128) NOT NULL,
  chat_id VARCHAR(128) NOT NULL DEFAULT '',
  status VARCHAR(32) NOT NULL DEFAULT 'active',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_users_tenant_open_id (tenant_id, feishu_open_id),
  KEY idx_users_tenant_status (tenant_id, status),
  CONSTRAINT fk_users_tenant FOREIGN KEY (tenant_id) REFERENCES tenants (tenant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS foods (
  food_id VARCHAR(80) PRIMARY KEY,
  tenant_id VARCHAR(80) NULL,
  user_id VARCHAR(80) NULL,
  scope VARCHAR(16) NOT NULL DEFAULT 'user',
  name VARCHAR(255) NOT NULL,
  carbs_per_100g DECIMAL(10, 2) NULL,
  protein_per_100g DECIMAL(10, 2) NULL,
  fat_per_100g DECIMAL(10, 2) NULL,
  kcal_per_100g DECIMAL(10, 2) NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'active',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_foods_user_name (tenant_id, user_id, name),
  KEY idx_foods_scope_status (scope, status),
  CONSTRAINT fk_foods_tenant FOREIGN KEY (tenant_id) REFERENCES tenants (tenant_id),
  CONSTRAINT fk_foods_user FOREIGN KEY (user_id) REFERENCES users (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS food_aliases (
  alias_id VARCHAR(80) PRIMARY KEY,
  tenant_id VARCHAR(80) NULL,
  user_id VARCHAR(80) NULL,
  food_id VARCHAR(80) NOT NULL,
  alias VARCHAR(255) NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'active',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_food_aliases_user_alias (tenant_id, user_id, alias),
  KEY idx_food_aliases_food (food_id, status),
  CONSTRAINT fk_food_aliases_food FOREIGN KEY (food_id) REFERENCES foods (food_id),
  CONSTRAINT fk_food_aliases_tenant FOREIGN KEY (tenant_id) REFERENCES tenants (tenant_id),
  CONSTRAINT fk_food_aliases_user FOREIGN KEY (user_id) REFERENCES users (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS intake_records (
  record_id VARCHAR(80) PRIMARY KEY,
  tenant_id VARCHAR(80) NOT NULL,
  user_id VARCHAR(80) NOT NULL,
  message_id VARCHAR(128) NOT NULL DEFAULT '',
  idempotency_key VARCHAR(160) NOT NULL,
  food_id VARCHAR(80) NULL,
  food_name_snapshot VARCHAR(255) NOT NULL,
  grams DECIMAL(10, 2) NOT NULL DEFAULT 0,
  kcal DECIMAL(10, 2) NOT NULL DEFAULT 0,
  carbs DECIMAL(10, 2) NOT NULL DEFAULT 0,
  protein DECIMAL(10, 2) NOT NULL DEFAULT 0,
  fat DECIMAL(10, 2) NOT NULL DEFAULT 0,
  meal_type VARCHAR(32) NOT NULL DEFAULT 'unknown',
  raw_text VARCHAR(1000) NOT NULL DEFAULT '',
  record_date DATE NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'active',
  delete_reason VARCHAR(255) NOT NULL DEFAULT '',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_intake_idempotency (tenant_id, user_id, idempotency_key),
  KEY idx_intake_user_date_status (tenant_id, user_id, record_date, status),
  KEY idx_intake_message_id (tenant_id, user_id, message_id),
  CONSTRAINT fk_intake_tenant FOREIGN KEY (tenant_id) REFERENCES tenants (tenant_id),
  CONSTRAINT fk_intake_user FOREIGN KEY (user_id) REFERENCES users (user_id),
  CONSTRAINT fk_intake_food FOREIGN KEY (food_id) REFERENCES foods (food_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS standards (
  standard_id VARCHAR(80) PRIMARY KEY,
  tenant_id VARCHAR(80) NOT NULL,
  user_id VARCHAR(80) NOT NULL,
  standard_date DATE NOT NULL,
  kcal DECIMAL(10, 2) NOT NULL DEFAULT 2230,
  carbs DECIMAL(10, 2) NOT NULL DEFAULT 250,
  protein DECIMAL(10, 2) NOT NULL DEFAULT 150,
  fat DECIMAL(10, 2) NOT NULL DEFAULT 70,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_standards_user_date (tenant_id, user_id, standard_date),
  CONSTRAINT fk_standards_tenant FOREIGN KEY (tenant_id) REFERENCES tenants (tenant_id),
  CONSTRAINT fk_standards_user FOREIGN KEY (user_id) REFERENCES users (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS memories (
  memory_id VARCHAR(80) PRIMARY KEY,
  tenant_id VARCHAR(80) NOT NULL,
  user_id VARCHAR(80) NOT NULL,
  memory_type VARCHAR(64) NOT NULL DEFAULT 'planner_hint',
  memory_key VARCHAR(128) NOT NULL,
  value_json JSON NOT NULL,
  confidence DECIMAL(4, 3) NOT NULL DEFAULT 1.000,
  status VARCHAR(32) NOT NULL DEFAULT 'active',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_memories_user_key (tenant_id, user_id, memory_type, memory_key),
  KEY idx_memories_user_status (tenant_id, user_id, status),
  CONSTRAINT fk_memories_tenant FOREIGN KEY (tenant_id) REFERENCES tenants (tenant_id),
  CONSTRAINT fk_memories_user FOREIGN KEY (user_id) REFERENCES users (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS usage_events (
  event_id VARCHAR(80) PRIMARY KEY,
  tenant_id VARCHAR(80) NOT NULL,
  user_id VARCHAR(80) NOT NULL,
  event_type VARCHAR(64) NOT NULL,
  success TINYINT(1) NOT NULL DEFAULT 1,
  latency_ms INT NOT NULL DEFAULT 0,
  error_type VARCHAR(128) NOT NULL DEFAULT '',
  message_id VARCHAR(128) NOT NULL DEFAULT '',
  metadata_json JSON NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_usage_user_time (tenant_id, user_id, created_at),
  KEY idx_usage_event_type (event_type, success, created_at),
  CONSTRAINT fk_usage_tenant FOREIGN KEY (tenant_id) REFERENCES tenants (tenant_id),
  CONSTRAINT fk_usage_user FOREIGN KEY (user_id) REFERENCES users (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS feedback_samples (
  feedback_id VARCHAR(80) PRIMARY KEY,
  tenant_id VARCHAR(80) NOT NULL,
  user_id VARCHAR(80) NOT NULL,
  message_id VARCHAR(128) NOT NULL DEFAULT '',
  feedback_type VARCHAR(64) NOT NULL DEFAULT 'general',
  content VARCHAR(2000) NOT NULL,
  sentiment VARCHAR(32) NOT NULL DEFAULT 'unknown',
  status VARCHAR(32) NOT NULL DEFAULT 'open',
  metadata_json JSON NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_feedback_user_time (tenant_id, user_id, created_at),
  KEY idx_feedback_status (status, created_at),
  CONSTRAINT fk_feedback_tenant FOREIGN KEY (tenant_id) REFERENCES tenants (tenant_id),
  CONSTRAINT fk_feedback_user FOREIGN KEY (user_id) REFERENCES users (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
