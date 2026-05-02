CREATE TABLE IF NOT EXISTS blueprint_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    reseller_id VARCHAR(255) NOT NULL,
    merchant_id VARCHAR(255),
    mode VARCHAR(20) NOT NULL DEFAULT 'create',
    template_id UUID,
    langgraph_thread_id VARCHAR(255) NOT NULL,
    current_step VARCHAR(50),
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    result_template_id UUID,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    expires_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() + INTERVAL '7 days'
);

CREATE INDEX IF NOT EXISTS idx_blueprint_sessions_user_id ON blueprint_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_blueprint_sessions_status ON blueprint_sessions(status);
