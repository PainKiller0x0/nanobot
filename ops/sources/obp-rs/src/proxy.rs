use crate::config::{save_config, Channel, RouterConfig};
use crate::stats::{save_stats, RequestLog, TokenUsage, UsageStats};
use axum::{
    body::{to_bytes, Body},
    extract::State,
    http::{header, HeaderMap, HeaderName, Request, Response, StatusCode},
    response::IntoResponse,
};
use reqwest::{Body as ReqBody, Client, RequestBuilder};
use serde_json::Value;
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tokio::sync::Mutex;

const MAX_REQUEST_BYTES: usize = 16 * 1024 * 1024;

pub struct ProxyState {
    pub client: Client,
    pub channels: Mutex<Vec<Channel>>,
    pub router: Mutex<RouterConfig>,
    pub stats: Mutex<UsageStats>,
    pub index: Mutex<usize>,
    pub config_path: String,
    pub router_path: String,
    pub stats_path: String,
}

#[derive(Debug, Clone)]
struct RouteDecision {
    requested_model: String,
    desired_model: String,
    role: String,
    group: String,
    reason: String,
}

#[derive(Debug, Clone)]
struct Attempt {
    channel: Channel,
    actual_model: String,
    role: String,
    group: String,
    reason: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ApiProtocol {
    OpenAI,
    Anthropic,
}

impl ApiProtocol {
    fn from_channel(ch: &Channel) -> Option<Self> {
        match ch.r#type.trim().to_lowercase().as_str() {
            "" | "openai" | "openai-compatible" => Some(Self::OpenAI),
            "anthropic" | "anthropic-api" => Some(Self::Anthropic),
            _ => None,
        }
    }

    fn channel_match_rank(ch: &Channel, client_protocol: Self) -> u8 {
        match Self::from_channel(ch) {
            Some(upstream) if upstream == client_protocol => 0,
            Some(_) => 1,
            None => 2,
        }
    }

    fn target_url(self, base: &str) -> String {
        match self {
            Self::OpenAI => openai_chat_url(base),
            Self::Anthropic => anthropic_messages_url(base),
        }
    }

    fn apply_channel_auth(self, req: RequestBuilder, channel: &Channel) -> RequestBuilder {
        match self {
            Self::OpenAI => req.header("Authorization", format!("Bearer {}", channel.key)),
            Self::Anthropic => {
                if channel.base.to_lowercase().contains("anthropic.com") {
                    req.header("x-api-key", &channel.key)
                        .header("anthropic-version", "2023-06-01")
                } else {
                    req.header("Authorization", format!("Bearer {}", channel.key))
                }
            }
        }
    }
}

#[derive(Debug, Clone, Default)]
struct RouteHints {
    purpose: String,
    intent: String,
}

impl RouteHints {
    fn from_request(headers: &HeaderMap, request_json: Option<&Value>) -> Self {
        Self {
            purpose: first_non_empty(&[
                header_hint(headers, "x-obp-purpose"),
                json_hint(request_json, &["obp_purpose", "x_obp_purpose", "purpose"]),
            ]),
            intent: first_non_empty(&[
                header_hint(headers, "x-obp-intent"),
                json_hint(request_json, &["obp_intent", "x_obp_intent", "intent"]),
            ]),
        }
    }

    fn pro_reason(&self) -> Option<String> {
        for (label, value) in [
            ("purpose", self.purpose.as_str()),
            ("intent", self.intent.as_str()),
        ] {
            if hint_matches(value, PRO_HINTS) {
                return Some(format!("{} hint matched: {}", label, value));
            }
        }
        None
    }

    fn light_reason(&self) -> Option<String> {
        for (label, value) in [
            ("purpose", self.purpose.as_str()),
            ("intent", self.intent.as_str()),
        ] {
            if hint_matches(value, LIGHTWEIGHT_HINTS) {
                return Some(format!("{} hint keeps default: {}", label, value));
            }
        }
        None
    }
}

const PRO_HINTS: &[&str] = &[
    "compact",
    "compression",
    "summarize",
    "summary",
    "memory",
    "reflection",
    "reflexio",
    "dream",
    "review",
    "code_review",
    "architecture",
    "design",
    "migration",
    "reasoning",
    "analysis",
    "diagnose",
    "troubleshoot",
    "root_cause",
];

const LIGHTWEIGHT_HINTS: &[&str] = &[
    "status",
    "health",
    "weather",
    "cron",
    "schedule",
    "lof",
    "rss",
    "quote",
    "simple",
    "simple_chat",
    "fast_chat",
];

const LIGHTWEIGHT_TEXT_PATTERNS: &[&str] = &[
    "heartbeat.md",
    "heartbeat agent",
    "heartbeat tool",
    "\"name\":\"heartbeat\"",
    "\"name\": \"heartbeat\"",
];

const PRO_TEXT_PATTERNS: &[&str] = &[
    "context compression",
    "context summary",
    "conversation summary",
    "memory consolidation",
    "compact conversation",
    "summarize conversation",
    "summarize this conversation",
    "summarize the conversation",
    "conversation so far",
    "consolidate memory",
    "code review",
    "review existing",
    "root cause",
    "上下文压缩",
    "压缩上下文",
    "总结对话",
    "对话总结",
    "记忆整理",
    "代码审查",
    "根因",
    "排障",
];

pub async fn handle_openai_proxy(
    State(state): State<Arc<ProxyState>>,
    req: Request<Body>,
) -> Response<Body> {
    handle_proxy(State(state), req, ApiProtocol::OpenAI).await
}

pub async fn handle_anthropic_proxy(
    State(state): State<Arc<ProxyState>>,
    req: Request<Body>,
) -> Response<Body> {
    handle_proxy(State(state), req, ApiProtocol::Anthropic).await
}

async fn handle_proxy(
    State(state): State<Arc<ProxyState>>,
    req: Request<Body>,
    protocol: ApiProtocol,
) -> Response<Body> {
    let started = Instant::now();
    let (parts, body) = req.into_parts();
    let body_bytes = match to_bytes(body, MAX_REQUEST_BYTES).await {
        Ok(bytes) => bytes,
        Err(e) => {
            return (
                StatusCode::BAD_REQUEST,
                format!("Invalid request body: {}", e),
            )
                .into_response();
        }
    };

    let request_json = serde_json::from_slice::<Value>(&body_bytes).ok();
    let requested_model = request_json
        .as_ref()
        .and_then(|v| v.get("model"))
        .and_then(Value::as_str)
        .unwrap_or("unknown")
        .to_string();
    let stream = request_json
        .as_ref()
        .and_then(|v| v.get("stream"))
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let route_hints = RouteHints::from_request(&parts.headers, request_json.as_ref());

    let router = state.router.lock().await.clone();
    if !router.external_enabled {
        return error_response(StatusCode::FORBIDDEN, "external_access_disabled");
    }
    if !external_model_allowed(&router, &requested_model) {
        return error_response(
            StatusCode::FORBIDDEN,
            &format!("model_not_allowed: {}", requested_model),
        );
    }

    let channels = state.channels.lock().await.clone();
    if channels.is_empty() {
        return (StatusCode::NOT_FOUND, "No channels available").into_response();
    }
    let stats = state.stats.lock().await.clone();
    let decision = route_decision(
        &router,
        &stats,
        request_json.as_ref(),
        &requested_model,
        &route_hints,
    );
    let attempts = build_attempts(&state, &channels, &router, &decision, protocol).await;
    if attempts.is_empty() {
        record_failure(
            &state,
            None,
            requested_model,
            decision.desired_model,
            decision.role,
            decision.reason,
            StatusCode::NOT_FOUND.as_u16(),
            started.elapsed(),
        )
        .await;
        return (StatusCode::NOT_FOUND, "No active channels available").into_response();
    }

    let retry_statuses = router.retry_statuses.clone();
    let mut last_error: Option<Response<Body>> = None;
    for (attempt_idx, attempt) in attempts.iter().enumerate() {
        let Some(upstream_protocol) = ApiProtocol::from_channel(&attempt.channel) else {
            continue;
        };
        if stream && protocol != upstream_protocol {
            continue;
        }
        let target_url = upstream_protocol.target_url(&attempt.channel.base);
        let attempt_body = rewrite_body_for_upstream(
            &body_bytes,
            &attempt.actual_model,
            protocol,
            upstream_protocol,
        );
        let mut target_req = state
            .client
            .post(&target_url)
            .body(ReqBody::from(attempt_body));

        for (name, value) in parts.headers.iter() {
            if name != "host"
                && name != "authorization"
                && name != "content-length"
                && !name.as_str().eq_ignore_ascii_case("x-api-key")
                && !name.as_str().to_ascii_lowercase().starts_with("x-obp-")
            {
                target_req = target_req.header(name, value);
            }
        }
        target_req = upstream_protocol.apply_channel_auth(target_req, &attempt.channel);

        let response = match target_req.send().await {
            Ok(res) => res,
            Err(e) => {
                record_result(
                    &state,
                    &attempt.channel,
                    &decision.requested_model,
                    &attempt.actual_model,
                    &attempt.role,
                    &format!("{}; upstream error: {}", attempt.reason, e),
                    StatusCode::BAD_GATEWAY.as_u16(),
                    started.elapsed(),
                    TokenUsage::default(),
                )
                .await;
                continue;
            }
        };

        let status = StatusCode::from_u16(response.status().as_u16())
            .unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
        let status_u16 = status.as_u16();
        let retryable = retry_statuses.contains(&status_u16);

        if stream {
            if retryable && attempt_idx + 1 < attempts.len() {
                record_result(
                    &state,
                    &attempt.channel,
                    &decision.requested_model,
                    &attempt.actual_model,
                    &attempt.role,
                    &format!("{}; retryable status {}", attempt.reason, status_u16),
                    status_u16,
                    started.elapsed(),
                    TokenUsage::default(),
                )
                .await;
                continue;
            }
            record_result(
                &state,
                &attempt.channel,
                &decision.requested_model,
                &attempt.actual_model,
                &attempt.role,
                &attempt.reason,
                status_u16,
                started.elapsed(),
                TokenUsage::default(),
            )
            .await;
            let mut res_builder = response_with_headers(status, response.headers());
            res_builder = route_headers(res_builder, attempt, &decision);
            let res_stream = response.bytes_stream();
            return res_builder
                .body(Body::from_stream(res_stream))
                .unwrap_or_else(|_| {
                    (StatusCode::INTERNAL_SERVER_ERROR, "Internal Error").into_response()
                });
        }

        let headers = response.headers().clone();
        let response_bytes = match response.bytes().await {
            Ok(bytes) => bytes,
            Err(e) => {
                record_result(
                    &state,
                    &attempt.channel,
                    &decision.requested_model,
                    &attempt.actual_model,
                    &attempt.role,
                    &format!("{}; body error: {}", attempt.reason, e),
                    StatusCode::BAD_GATEWAY.as_u16(),
                    started.elapsed(),
                    TokenUsage::default(),
                )
                .await;
                continue;
            }
        };
        let response_bytes =
            rewrite_response_for_client(&response_bytes, status, protocol, upstream_protocol);
        let usage = TokenUsage::from_response_bytes(&response_bytes);
        record_result(
            &state,
            &attempt.channel,
            &decision.requested_model,
            &attempt.actual_model,
            &attempt.role,
            &attempt.reason,
            status_u16,
            started.elapsed(),
            usage,
        )
        .await;

        let mut res_builder = if protocol == upstream_protocol {
            response_with_headers(status, &headers)
        } else {
            Response::builder()
                .status(status)
                .header(header::CONTENT_TYPE, "application/json; charset=utf-8")
        };
        res_builder = route_headers(res_builder, attempt, &decision);
        let response = res_builder
            .body(Body::from(response_bytes.clone()))
            .unwrap_or_else(|_| {
                (StatusCode::INTERNAL_SERVER_ERROR, "Internal Error").into_response()
            });

        if retryable && attempt_idx + 1 < attempts.len() {
            last_error = Some(response);
            continue;
        }
        return response;
    }

    last_error.unwrap_or_else(|| {
        (
            StatusCode::BAD_GATEWAY,
            "All OBP upstream attempts failed".to_string(),
        )
            .into_response()
    })
}

fn error_response(status: StatusCode, message: &str) -> axum::response::Response {
    (
        status,
        [("content-type", "application/json; charset=utf-8")],
        serde_json::json!({
            "error": {
                "message": message,
                "type": "obp_router_error",
            }
        })
        .to_string(),
    )
        .into_response()
}

fn external_model_allowed(router: &RouterConfig, model: &str) -> bool {
    let allowed = &router.external_allowed_models;
    if allowed.is_empty() {
        return true;
    }
    let target = model.trim().to_lowercase();
    allowed
        .iter()
        .map(|item| item.trim())
        .filter(|item| !item.is_empty())
        .any(|pattern| model_pattern_matches(pattern, &target))
}

fn model_pattern_matches(pattern: &str, model: &str) -> bool {
    let pattern = pattern.to_lowercase();
    if pattern == "*" {
        return true;
    }
    if !pattern.contains('*') {
        return pattern == model;
    }
    let parts: Vec<&str> = pattern.split('*').filter(|part| !part.is_empty()).collect();
    if parts.is_empty() {
        return true;
    }
    let mut rest = model;
    for part in parts {
        let Some(idx) = rest.find(part) else {
            return false;
        };
        rest = &rest[idx + part.len()..];
    }
    true
}

fn route_decision(
    router: &RouterConfig,
    stats: &UsageStats,
    request_json: Option<&Value>,
    requested_model: &str,
    hints: &RouteHints,
) -> RouteDecision {
    if !router.enabled {
        return RouteDecision {
            requested_model: requested_model.to_string(),
            desired_model: requested_model.to_string(),
            role: "any".to_string(),
            group: String::new(),
            reason: "router disabled".to_string(),
        };
    }

    let monthly_cost = stats.current_month_cost();
    if router.monthly_hard_limit_rmb > 0.0 && monthly_cost >= router.monthly_hard_limit_rmb {
        let mut decision = RouteDecision {
            requested_model: requested_model.to_string(),
            desired_model: router.backup_model.clone(),
            role: "backup".to_string(),
            group: group_for_role(router, "backup"),
            reason: format!("monthly hard limit reached {:.2} CNY", monthly_cost),
        };
        if router.dry_run {
            decision.reason = format!(
                "dry-run: would use {}/{} because {}",
                decision.role, decision.desired_model, decision.reason
            );
            decision.desired_model = requested_model.to_string();
            decision.role = "any".to_string();
            decision.group.clear();
        }
        return decision;
    }

    if let Some(decision) = explicit_model_route(router, requested_model) {
        return decision;
    }

    let explicit_pro = contains_any(&requested_model.to_lowercase(), &["pro", "reasoner"]);
    let routing_text = request_json
        .map(extract_routing_text)
        .unwrap_or_default()
        .to_lowercase();
    let pro_text_hit = PRO_TEXT_PATTERNS
        .iter()
        .find(|pattern| routing_text.contains(**pattern))
        .map(|pattern| (*pattern).to_string());
    let light_text_hit = LIGHTWEIGHT_TEXT_PATTERNS
        .iter()
        .find(|pattern| routing_text.contains(**pattern))
        .map(|pattern| (*pattern).to_string());
    let keyword_hit = router
        .pro_keywords
        .iter()
        .find(|keyword| routing_text.contains(&keyword.to_lowercase()))
        .cloned();

    let mut wants_pro = explicit_pro;
    let mut reason = if explicit_pro {
        "requested pro/reasoner model".to_string()
    } else {
        "default lightweight route".to_string()
    };
    if !wants_pro {
        if let Some(pro_reason) = hints.pro_reason() {
            wants_pro = true;
            reason = pro_reason;
        }
    }
    if !wants_pro {
        if let Some(light_reason) = hints.light_reason() {
            reason = light_reason;
        }
    }
    if !wants_pro {
        if let Some(pattern) = light_text_hit.as_deref() {
            reason = format!("task pattern keeps default: {}", pattern);
        }
    }
    if !wants_pro && light_text_hit.is_none() && !hint_matches(&hints.intent, LIGHTWEIGHT_HINTS) {
        if let Some(pattern) = pro_text_hit {
            wants_pro = true;
            reason = format!("task pattern matched: {}", pattern);
        }
    }
    if !wants_pro && light_text_hit.is_none() && !hint_matches(&hints.intent, LIGHTWEIGHT_HINTS) {
        if let Some(keyword) = keyword_hit {
            wants_pro = true;
            reason = format!("keyword matched: {}", keyword);
        }
    }

    let budget_downgrade =
        router.monthly_downgrade_rmb > 0.0 && monthly_cost >= router.monthly_downgrade_rmb;
    let mut decision = if budget_downgrade && wants_pro {
        RouteDecision {
            requested_model: requested_model.to_string(),
            desired_model: router.default_model.clone(),
            role: "default".to_string(),
            group: group_for_role(router, "default"),
            reason: format!(
                "monthly downgrade threshold reached {:.2} CNY; suppressed pro route ({})",
                monthly_cost, reason
            ),
        }
    } else if wants_pro {
        RouteDecision {
            requested_model: requested_model.to_string(),
            desired_model: router.pro_model.clone(),
            role: "pro".to_string(),
            group: group_for_role(router, "pro"),
            reason,
        }
    } else {
        RouteDecision {
            requested_model: requested_model.to_string(),
            desired_model: router.default_model.clone(),
            role: "default".to_string(),
            group: group_for_role(router, "default"),
            reason,
        }
    };

    if router.dry_run {
        decision.reason = format!(
            "dry-run: would use {}/{} because {}",
            decision.role, decision.desired_model, decision.reason
        );
        decision.desired_model = requested_model.to_string();
        decision.role = "any".to_string();
        decision.group.clear();
    }

    decision
}

fn explicit_model_route(router: &RouterConfig, requested_model: &str) -> Option<RouteDecision> {
    let requested = requested_model.trim();
    if requested.is_empty() || requested.eq_ignore_ascii_case("unknown") {
        return None;
    }
    if model_eq(requested, &router.default_model) {
        // The default model remains smart-routable: long/complex prompts may still upgrade to Pro.
        return None;
    }
    if model_eq(requested, &router.pro_model) {
        return Some(RouteDecision {
            requested_model: requested.to_string(),
            desired_model: router.pro_model.clone(),
            role: "pro".to_string(),
            group: group_for_role(router, "pro"),
            reason: "requested configured pro model".to_string(),
        });
    }
    if model_eq(requested, &router.emergency_model) {
        return Some(RouteDecision {
            requested_model: requested.to_string(),
            desired_model: router.emergency_model.clone(),
            role: "emergency".to_string(),
            group: group_for_role(router, "emergency"),
            reason: "requested configured emergency model".to_string(),
        });
    }
    if model_eq(requested, &router.backup_model) {
        return Some(RouteDecision {
            requested_model: requested.to_string(),
            desired_model: router.backup_model.clone(),
            role: "backup".to_string(),
            group: group_for_role(router, "backup"),
            reason: "requested configured backup model".to_string(),
        });
    }
    Some(RouteDecision {
        requested_model: requested.to_string(),
        desired_model: requested.to_string(),
        role: "any".to_string(),
        group: String::new(),
        reason: "requested explicit model passthrough".to_string(),
    })
}

fn model_eq(a: &str, b: &str) -> bool {
    !b.trim().is_empty() && a.trim().eq_ignore_ascii_case(b.trim())
}

fn group_for_role(router: &RouterConfig, role: &str) -> String {
    match role {
        "default" => router.default_group.trim(),
        "pro" => router.pro_group.trim(),
        "emergency" => router.emergency_group.trim(),
        "backup" => router.backup_group.trim(),
        _ => "",
    }
    .to_lowercase()
}

#[derive(Debug, Clone)]
struct AttemptSpec {
    role: String,
    group: String,
    desired_model: String,
    fallback: bool,
}

async fn build_attempts(
    state: &Arc<ProxyState>,
    channels: &[Channel],
    router: &RouterConfig,
    decision: &RouteDecision,
    protocol: ApiProtocol,
) -> Vec<Attempt> {
    let mut specs = Vec::new();
    add_role_attempts(
        &mut specs,
        router,
        decision.role.clone(),
        decision.desired_model.clone(),
        false,
    );

    for &role in fallback_roles(&decision.role) {
        if role == "any" {
            add_attempt_spec(
                &mut specs,
                "any".to_string(),
                String::new(),
                decision.desired_model.clone(),
                true,
            );
        } else {
            add_role_attempts(
                &mut specs,
                router,
                role.to_string(),
                model_for_role(router, role).to_string(),
                true,
            );
        }
    }
    let mut attempts = Vec::new();
    for spec in specs {
        let mut candidates: Vec<Channel> = channels
            .iter()
            .filter(|ch| ch.is_active())
            .filter(|ch| ApiProtocol::from_channel(ch).is_some())
            .filter(|ch| spec.role == "any" || ch.role_key() == spec.role)
            .filter(|ch| spec.group.is_empty() || ch.group_key() == spec.group)
            .filter(|ch| {
                ch.supports_model(&spec.desired_model)
                    || ch.supports_model(&decision.requested_model)
            })
            .cloned()
            .collect();
        candidates.sort_by_key(|ch| {
            (
                ApiProtocol::channel_match_rank(ch, protocol),
                ch.priority,
                ch.group_key(),
                ch.name.clone(),
            )
        });
        rotate_candidates(state, &mut candidates).await;
        for ch in candidates {
            let actual = ch.mapped_model(&decision.requested_model, &spec.desired_model);
            let attempt = Attempt {
                channel: ch,
                actual_model: actual,
                role: spec.role.clone(),
                group: spec.group.clone(),
                reason: if !spec.fallback
                    && spec.role == decision.role
                    && spec.group == decision.group
                {
                    decision.reason.clone()
                } else if spec.group.is_empty() {
                    format!("fallback to {}", spec.role)
                } else {
                    format!("fallback to {}/{}", spec.role, spec.group)
                },
            };
            if !attempts.iter().any(|existing: &Attempt| {
                existing.channel.id == attempt.channel.id
                    && existing.actual_model == attempt.actual_model
            }) {
                attempts.push(attempt);
            }
        }
    }
    attempts
}

fn fallback_roles(role: &str) -> &'static [&'static str] {
    match role {
        // When the monthly hard limit is reached, save the emergency pool for true incidents.
        "backup" => &["emergency", "any"],
        // Normal traffic should fail over to emergency first because this means the main pool timed out or errored.
        "default" | "pro" => &["emergency", "backup", "any"],
        "emergency" => &["backup", "any"],
        _ => &["any"],
    }
}

fn model_for_role<'a>(router: &'a RouterConfig, role: &str) -> &'a str {
    match role {
        "pro" => router.pro_model.as_str(),
        "emergency" => router.emergency_model.as_str(),
        "backup" => router.backup_model.as_str(),
        "default" => router.default_model.as_str(),
        _ => router.default_model.as_str(),
    }
}

fn add_role_attempts(
    specs: &mut Vec<AttemptSpec>,
    router: &RouterConfig,
    role: String,
    desired_model: String,
    fallback: bool,
) {
    let group = group_for_role(router, &role);
    add_attempt_spec(
        specs,
        role.clone(),
        group.clone(),
        desired_model.clone(),
        fallback,
    );
    if !group.is_empty() {
        add_attempt_spec(specs, role, String::new(), desired_model, true);
    }
}

fn add_attempt_spec(
    specs: &mut Vec<AttemptSpec>,
    role: String,
    group: String,
    desired_model: String,
    fallback: bool,
) {
    if specs
        .iter()
        .any(|item| item.role == role && item.group == group && item.desired_model == desired_model)
    {
        return;
    }
    specs.push(AttemptSpec {
        role,
        group,
        desired_model,
        fallback,
    });
}

async fn rotate_candidates(state: &Arc<ProxyState>, candidates: &mut [Channel]) {
    if candidates.len() <= 1 {
        return;
    }
    let mut idx = state.index.lock().await;
    let offset = *idx % candidates.len();
    *idx = idx.saturating_add(1);
    candidates.rotate_left(offset);
}

fn rewrite_model(body: &[u8], model: &str) -> Vec<u8> {
    let Ok(mut value) = serde_json::from_slice::<Value>(body) else {
        return body.to_vec();
    };
    if let Some(obj) = value.as_object_mut() {
        obj.insert("model".to_string(), Value::String(model.to_string()));
        return serde_json::to_vec(&value).unwrap_or_else(|_| body.to_vec());
    }
    body.to_vec()
}

fn rewrite_body_for_upstream(
    body: &[u8],
    model: &str,
    client_protocol: ApiProtocol,
    upstream_protocol: ApiProtocol,
) -> Vec<u8> {
    match (client_protocol, upstream_protocol) {
        (ApiProtocol::OpenAI, ApiProtocol::OpenAI) => rewrite_openai_model(body, model),
        (ApiProtocol::Anthropic, ApiProtocol::Anthropic) => rewrite_model(body, model),
        (ApiProtocol::Anthropic, ApiProtocol::OpenAI) => anthropic_request_to_openai(body, model),
        (ApiProtocol::OpenAI, ApiProtocol::Anthropic) => openai_request_to_anthropic(body, model),
    }
}

fn rewrite_openai_model(body: &[u8], model: &str) -> Vec<u8> {
    let Ok(mut value) = serde_json::from_slice::<Value>(body) else {
        return body.to_vec();
    };
    if let Some(obj) = value.as_object_mut() {
        obj.insert("model".to_string(), Value::String(model.to_string()));
        apply_openai_model_defaults(&mut value, model);
        return serde_json::to_vec(&value).unwrap_or_else(|_| body.to_vec());
    }
    body.to_vec()
}

fn anthropic_request_to_openai(body: &[u8], model: &str) -> Vec<u8> {
    let Ok(value) = serde_json::from_slice::<Value>(body) else {
        return rewrite_openai_model(body, model);
    };
    let mut messages = Vec::new();
    if let Some(system) = value.get("system") {
        let system_text = content_to_text(system);
        if !system_text.is_empty() {
            messages.push(serde_json::json!({"role": "system", "content": system_text}));
        }
    }
    if let Some(items) = value.get("messages").and_then(Value::as_array) {
        for item in items {
            let role = item.get("role").and_then(Value::as_str).unwrap_or("user");
            let content = item.get("content").map(content_to_text).unwrap_or_default();
            messages.push(serde_json::json!({"role": role, "content": content}));
        }
    }

    let mut out = serde_json::json!({
        "model": model,
        "messages": messages,
    });
    copy_json_fields(
        &value,
        &mut out,
        &[
            ("max_tokens", "max_tokens"),
            ("temperature", "temperature"),
            ("top_p", "top_p"),
            ("stream", "stream"),
            ("stop_sequences", "stop"),
        ],
    );
    apply_openai_model_defaults(&mut out, model);
    json_bytes_or(&out, rewrite_openai_model(body, model))
}

fn apply_openai_model_defaults(value: &mut Value, model: &str) {
    if !model.to_lowercase().contains("deepseek-v4-pro") {
        return;
    }
    let Some(obj) = value.as_object_mut() else {
        return;
    };
    obj.entry("thinking".to_string())
        .or_insert_with(|| serde_json::json!({"type": "enabled"}));
    obj.entry("reasoning_effort".to_string())
        .or_insert_with(|| Value::String("high".to_string()));
}

fn openai_request_to_anthropic(body: &[u8], model: &str) -> Vec<u8> {
    let Ok(value) = serde_json::from_slice::<Value>(body) else {
        return rewrite_model(body, model);
    };
    let mut messages = Vec::new();
    let mut system_parts = Vec::new();
    if let Some(items) = value.get("messages").and_then(Value::as_array) {
        for item in items {
            let role = item.get("role").and_then(Value::as_str).unwrap_or("user");
            let content = item.get("content").map(content_to_text).unwrap_or_default();
            if role == "system" {
                if !content.is_empty() {
                    system_parts.push(content);
                }
                continue;
            }
            let anthropic_role = if role == "assistant" {
                "assistant"
            } else {
                "user"
            };
            messages.push(serde_json::json!({"role": anthropic_role, "content": content}));
        }
    }
    let max_tokens = value
        .get("max_tokens")
        .or_else(|| value.get("max_completion_tokens"))
        .cloned()
        .unwrap_or(Value::from(4096));
    let mut out = serde_json::json!({
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    });
    if !system_parts.is_empty() {
        out["system"] = Value::String(system_parts.join("\n\n"));
    }
    copy_json_fields(
        &value,
        &mut out,
        &[
            ("temperature", "temperature"),
            ("top_p", "top_p"),
            ("stream", "stream"),
            ("stop", "stop_sequences"),
        ],
    );
    json_bytes_or(&out, rewrite_model(body, model))
}

fn copy_json_fields(from: &Value, to: &mut Value, fields: &[(&str, &str)]) {
    let Some(obj) = to.as_object_mut() else {
        return;
    };
    for (src, dst) in fields {
        if let Some(value) = from.get(*src) {
            obj.insert((*dst).to_string(), value.clone());
        }
    }
}

fn json_bytes_or(value: &Value, fallback: Vec<u8>) -> Vec<u8> {
    serde_json::to_vec(value).unwrap_or(fallback)
}

fn content_to_text(value: &Value) -> String {
    if let Some(text) = value.as_str() {
        return text.to_string();
    }
    let Some(items) = value.as_array() else {
        return value.to_string();
    };
    items
        .iter()
        .filter_map(|item| {
            if let Some(text) = item.as_str() {
                return Some(text.to_string());
            }
            match item.get("type").and_then(Value::as_str) {
                Some("text") => item
                    .get("text")
                    .and_then(Value::as_str)
                    .map(ToString::to_string),
                Some("image") | Some("image_url") => Some("[image]".to_string()),
                _ => item
                    .get("text")
                    .and_then(Value::as_str)
                    .map(ToString::to_string),
            }
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn rewrite_response_for_client(
    body: &[u8],
    status: StatusCode,
    client_protocol: ApiProtocol,
    upstream_protocol: ApiProtocol,
) -> Vec<u8> {
    if client_protocol == upstream_protocol || !status.is_success() {
        return body.to_vec();
    }
    match (client_protocol, upstream_protocol) {
        (ApiProtocol::Anthropic, ApiProtocol::OpenAI) => openai_response_to_anthropic(body),
        (ApiProtocol::OpenAI, ApiProtocol::Anthropic) => anthropic_response_to_openai(body),
        _ => body.to_vec(),
    }
}

fn openai_response_to_anthropic(body: &[u8]) -> Vec<u8> {
    let Ok(value) = serde_json::from_slice::<Value>(body) else {
        return body.to_vec();
    };
    let choice = value
        .get("choices")
        .and_then(Value::as_array)
        .and_then(|items| items.first());
    let message = choice.and_then(|item| item.get("message"));
    let text = message
        .and_then(|msg| msg.get("content"))
        .map(content_to_text)
        .unwrap_or_default();
    let reasoning = message
        .and_then(|msg| msg.get("reasoning_content"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let mut content = Vec::new();
    if !reasoning.is_empty() {
        content.push(serde_json::json!({"type": "thinking", "thinking": reasoning}));
    }
    content.push(serde_json::json!({"type": "text", "text": text}));
    let usage = value.get("usage").cloned().unwrap_or(Value::Null);
    let input_tokens = first_u64_in_value(&usage, &[&["prompt_tokens"], &["input_tokens"]]);
    let output_tokens = first_u64_in_value(&usage, &[&["completion_tokens"], &["output_tokens"]]);
    let cache_read_input_tokens = first_u64_in_value(
        &usage,
        &[
            &["prompt_tokens_details", "cached_tokens"],
            &["input_tokens_details", "cached_tokens"],
            &["cache_read_input_tokens"],
        ],
    );
    let out = serde_json::json!({
        "id": value.get("id").cloned().unwrap_or_else(|| Value::String(format!("msg_{}", now_secs()))),
        "type": "message",
        "role": "assistant",
        "model": value.get("model").cloned().unwrap_or_else(|| Value::String("unknown".to_string())),
        "content": content,
        "stop_reason": mapped_reason(
            choice.and_then(|item| item.get("finish_reason")).and_then(Value::as_str),
            &[("length", "max_tokens"), ("tool_calls", "tool_use")],
            "end_turn",
        ),
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": cache_read_input_tokens,
        }
    });
    json_bytes_or(&out, body.to_vec())
}

fn anthropic_response_to_openai(body: &[u8]) -> Vec<u8> {
    let Ok(value) = serde_json::from_slice::<Value>(body) else {
        return body.to_vec();
    };
    let content = value
        .get("content")
        .map(content_to_text)
        .unwrap_or_default();
    let usage = value.get("usage").cloned().unwrap_or(Value::Null);
    let prompt_tokens = first_u64_in_value(&usage, &[&["input_tokens"], &["prompt_tokens"]]);
    let completion_tokens =
        first_u64_in_value(&usage, &[&["output_tokens"], &["completion_tokens"]]);
    let cached_tokens = first_u64_in_value(
        &usage,
        &[
            &["cache_read_input_tokens"],
            &["cached_tokens"],
            &["prompt_tokens_details", "cached_tokens"],
        ],
    );
    let out = serde_json::json!({
        "id": value.get("id").cloned().unwrap_or_else(|| Value::String(format!("chatcmpl-{}", now_secs()))),
        "object": "chat.completion",
        "created": now_secs(),
        "model": value.get("model").cloned().unwrap_or_else(|| Value::String("unknown".to_string())),
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": mapped_reason(
                value.get("stop_reason").and_then(Value::as_str),
                &[("max_tokens", "length"), ("tool_use", "tool_calls")],
                "stop",
            ),
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens.saturating_add(completion_tokens),
            "prompt_tokens_details": {"cached_tokens": cached_tokens},
        }
    });
    json_bytes_or(&out, body.to_vec())
}

fn first_u64_in_value(value: &Value, paths: &[&[&str]]) -> u64 {
    for path in paths {
        let mut cur = value;
        for key in *path {
            let Some(next) = cur.get(*key) else {
                cur = &Value::Null;
                break;
            };
            cur = next;
        }
        if let Some(n) = cur.as_u64() {
            return n;
        }
    }
    0
}

fn mapped_reason(reason: Option<&str>, mappings: &[(&str, &str)], default: &str) -> Value {
    Value::String(
        mappings
            .iter()
            .find_map(|(from, to)| (reason == Some(*from)).then_some(*to))
            .unwrap_or(default)
            .to_string(),
    )
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn openai_chat_url(base: &str) -> String {
    endpoint_url(base, "chat/completions")
}

fn anthropic_messages_url(base: &str) -> String {
    endpoint_url(base, "messages")
}

fn endpoint_url(base: &str, endpoint: &str) -> String {
    let base = base.trim_end_matches('/');
    if base.ends_with(endpoint) {
        base.to_string()
    } else if base.ends_with("/v1") {
        format!("{}/{}", base, endpoint)
    } else {
        format!("{}/v1/{}", base, endpoint)
    }
}

fn response_with_headers(
    status: StatusCode,
    headers: &reqwest::header::HeaderMap,
) -> axum::http::response::Builder {
    let mut builder = Response::builder().status(status);
    for (name, value) in headers.iter() {
        if name != header::CONTENT_LENGTH {
            builder = builder.header(name, value);
        }
    }
    builder
}

fn route_headers(
    mut builder: axum::http::response::Builder,
    attempt: &Attempt,
    decision: &RouteDecision,
) -> axum::http::response::Builder {
    let headers = [
        ("x-obp-route", attempt.role.as_str()),
        ("x-obp-group", attempt.group.as_str()),
        ("x-obp-requested-model", decision.requested_model.as_str()),
        ("x-obp-actual-model", attempt.actual_model.as_str()),
        ("x-obp-channel", attempt.channel.name.as_str()),
        ("x-obp-reason", attempt.reason.as_str()),
    ];
    for (name, value) in headers {
        if let Ok(header_name) = HeaderName::from_bytes(name.as_bytes()) {
            builder = builder.header(header_name, value);
        }
    }
    builder
}

async fn record_failure(
    state: &Arc<ProxyState>,
    channel: Option<&Channel>,
    requested_model: String,
    actual_model: String,
    route: String,
    reason: String,
    status: u16,
    elapsed: Duration,
) {
    if let Some(ch) = channel {
        record_result(
            state,
            ch,
            &requested_model,
            &actual_model,
            &route,
            &reason,
            status,
            elapsed,
            TokenUsage::default(),
        )
        .await;
    }
}

#[allow(clippy::too_many_arguments)]
async fn record_result(
    state: &Arc<ProxyState>,
    ch: &Channel,
    requested_model: &str,
    actual_model: &str,
    route: &str,
    route_reason: &str,
    status: u16,
    elapsed: Duration,
    usage: TokenUsage,
) {
    let latency_ms = elapsed.as_millis().min(u128::from(u64::MAX)) as u64;
    let log = RequestLog::new(
        ch.id,
        ch.name.clone(),
        requested_model.to_string(),
        actual_model.to_string(),
        route.to_string(),
        route_reason.to_string(),
        status,
        latency_ms,
        usage,
    );
    log_model_route(&log, ch);

    {
        let mut channels = state.channels.lock().await;
        if let Some(current) = channels
            .iter_mut()
            .find(|item| item.id == ch.id && item.name == ch.name)
        {
            current.requests = current.requests.saturating_add(1);
            if (200..400).contains(&status) {
                current.fail_count = 0;
                current.status = "active".to_string();
            } else {
                current.fail_count = current.fail_count.saturating_add(1);
                if current.fail_count >= 3 {
                    current.status = "error".to_string();
                }
            }
        }
        save_config(&state.config_path, &channels);
    }

    {
        let mut stats = state.stats.lock().await;
        stats.record(log);
        save_stats(&state.stats_path, &stats);
    }
}

fn log_model_route(log: &RequestLog, ch: &Channel) {
    let group = if ch.group.trim().is_empty() {
        "-"
    } else {
        ch.group.trim()
    };
    if (200..400).contains(&log.status) {
        tracing::info!(
            target: "obp.model",
            time = %log.time,
            channel = %log.channel,
            group = %group,
            requested_model = %log.requested_model,
            actual_model = %log.model,
            route = %log.route,
            status = log.status,
            latency_ms = log.latency_ms,
            prompt_tokens = log.prompt_tokens,
            cached_tokens = log.cached_tokens,
            completion_tokens = log.completion_tokens,
            cost_cny = log.cost_cny,
            reason = %log.route_reason,
            "obp_model_route"
        );
    } else {
        tracing::warn!(
            target: "obp.model",
            time = %log.time,
            channel = %log.channel,
            group = %group,
            requested_model = %log.requested_model,
            actual_model = %log.model,
            route = %log.route,
            status = log.status,
            latency_ms = log.latency_ms,
            prompt_tokens = log.prompt_tokens,
            cached_tokens = log.cached_tokens,
            completion_tokens = log.completion_tokens,
            cost_cny = log.cost_cny,
            reason = %log.route_reason,
            "obp_model_route"
        );
    }
}

fn contains_any(text: &str, needles: &[&str]) -> bool {
    needles.iter().any(|needle| text.contains(needle))
}

fn hint_matches(value: &str, patterns: &[&str]) -> bool {
    let value = value.trim().to_lowercase();
    !value.is_empty()
        && patterns
            .iter()
            .any(|pattern| value.contains(&pattern.to_lowercase()))
}

fn first_non_empty(values: &[String]) -> String {
    values
        .iter()
        .find(|value| !value.trim().is_empty())
        .map(|value| value.trim().to_lowercase())
        .unwrap_or_default()
}

fn header_hint(headers: &HeaderMap, name: &str) -> String {
    headers
        .get(name)
        .and_then(|value| value.to_str().ok())
        .map(|value| value.trim().to_lowercase())
        .unwrap_or_default()
}

fn json_hint(value: Option<&Value>, keys: &[&str]) -> String {
    let Some(value) = value else {
        return String::new();
    };
    if let Some(found) = json_direct_hint(value, keys) {
        return found;
    }
    for container in ["metadata", "extra_body", "obp"] {
        if let Some(found) = value
            .get(container)
            .and_then(|inner| json_direct_hint(inner, keys))
        {
            return found;
        }
    }
    String::new()
}

fn json_direct_hint(value: &Value, keys: &[&str]) -> Option<String> {
    for key in keys {
        if let Some(text) = value.get(*key).and_then(Value::as_str) {
            let normalized = text.trim().to_lowercase();
            if !normalized.is_empty() {
                return Some(normalized);
            }
        }
    }
    None
}

fn extract_routing_text(value: &Value) -> String {
    if let Some(messages) = value.get("messages").and_then(Value::as_array) {
        if let Some(message) = messages
            .iter()
            .rev()
            .find(|message| message_role_is(message, "user"))
        {
            return message_content_text(message);
        }
        if let Some(message) = messages.last() {
            return message_content_text(message);
        }
    }
    if let Some(input) = value.get("input") {
        return extract_text(input);
    }
    extract_text(value)
}

fn message_role_is(message: &Value, expected: &str) -> bool {
    message
        .get("role")
        .and_then(Value::as_str)
        .map(|role| role.eq_ignore_ascii_case(expected))
        .unwrap_or(false)
}

fn message_content_text(message: &Value) -> String {
    message
        .get("content")
        .map(extract_text)
        .unwrap_or_else(|| extract_text(message))
}

fn extract_text(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Array(items) => items
            .iter()
            .map(extract_text)
            .collect::<Vec<_>>()
            .join("\n"),
        Value::Object(map) => map
            .iter()
            .filter(|(key, _)| key.as_str() != "tool_calls")
            .map(|(_, value)| extract_text(value))
            .collect::<Vec<_>>()
            .join("\n"),
        _ => String::new(),
    }
}
