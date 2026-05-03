use crate::config::{save_config, Channel, RouterConfig};
use crate::stats::{save_stats, RequestLog, TokenUsage, UsageStats};
use axum::{
    body::{to_bytes, Body},
    extract::State,
    http::{header, HeaderName, Request, Response, StatusCode},
    response::IntoResponse,
};
use reqwest::{Body as ReqBody, Client};
use serde_json::Value;
use std::sync::Arc;
use std::time::{Duration, Instant};
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

pub async fn handle_proxy(
    State(state): State<Arc<ProxyState>>,
    req: Request<Body>,
) -> impl IntoResponse {
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

    let channels = state.channels.lock().await.clone();
    if channels.is_empty() {
        return (StatusCode::NOT_FOUND, "No channels available").into_response();
    }
    let router = state.router.lock().await.clone();
    let stats = state.stats.lock().await.clone();
    let decision = route_decision(&router, &stats, request_json.as_ref(), &requested_model);
    let attempts = build_attempts(&state, &channels, &router, &decision).await;
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
        let target_url = chat_url(&attempt.channel.base);
        let attempt_body = rewrite_model(&body_bytes, &attempt.actual_model);
        let mut target_req = state
            .client
            .post(&target_url)
            .header("Authorization", format!("Bearer {}", attempt.channel.key))
            .body(ReqBody::from(attempt_body));

        for (name, value) in parts.headers.iter() {
            if name != "host" && name != "authorization" && name != "content-length" {
                target_req = target_req.header(name, value);
            }
        }

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

        let mut res_builder = response_with_headers(status, &headers);
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

fn route_decision(
    router: &RouterConfig,
    stats: &UsageStats,
    request_json: Option<&Value>,
    requested_model: &str,
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

    let explicit_pro = contains_any(
        &requested_model.to_lowercase(),
        &["pro", "reasoner", "deep"],
    );
    let prompt_chars = request_json.map(estimate_prompt_chars).unwrap_or(0);
    let message_count = request_json
        .and_then(|v| v.get("messages"))
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0);
    let full_text = request_json
        .map(extract_text)
        .unwrap_or_default()
        .to_lowercase();
    let keyword_hit = router
        .pro_keywords
        .iter()
        .find(|keyword| full_text.contains(&keyword.to_lowercase()))
        .cloned();

    let mut wants_pro = explicit_pro;
    let mut reason = if explicit_pro {
        "requested pro/reasoner model".to_string()
    } else {
        "default lightweight route".to_string()
    };
    if !wants_pro && prompt_chars >= router.pro_prompt_chars {
        wants_pro = true;
        reason = format!(
            "prompt chars {} >= {}",
            prompt_chars, router.pro_prompt_chars
        );
    }
    if !wants_pro && message_count >= router.pro_message_count {
        wants_pro = true;
        reason = format!(
            "message count {} >= {}",
            message_count, router.pro_message_count
        );
    }
    if !wants_pro {
        if let Some(keyword) = keyword_hit {
            wants_pro = true;
            reason = format!("keyword matched: {}", keyword);
        }
    }

    let mut decision =
        if router.monthly_downgrade_rmb > 0.0 && monthly_cost >= router.monthly_downgrade_rmb {
            RouteDecision {
                requested_model: requested_model.to_string(),
                desired_model: router.default_model.clone(),
                role: "default".to_string(),
                group: group_for_role(router, "default"),
                reason: format!(
                    "monthly downgrade threshold reached {:.2} CNY",
                    monthly_cost
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
            .filter(|ch| spec.role == "any" || ch.role_key() == spec.role)
            .filter(|ch| spec.group.is_empty() || ch.group_key() == spec.group)
            .filter(|ch| {
                ch.supports_model(&spec.desired_model)
                    || ch.supports_model(&decision.requested_model)
            })
            .cloned()
            .collect();
        candidates.sort_by_key(|ch| (ch.priority, ch.group_key(), ch.name.clone()));
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

fn chat_url(base: &str) -> String {
    let base = base.trim_end_matches('/');
    if base.ends_with("/chat/completions") {
        base.to_string()
    } else if base.ends_with("/v1") {
        format!("{}/chat/completions", base)
    } else {
        format!("{}/v1/chat/completions", base)
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

fn contains_any(text: &str, needles: &[&str]) -> bool {
    needles.iter().any(|needle| text.contains(needle))
}

fn estimate_prompt_chars(value: &Value) -> usize {
    extract_text(value).chars().count()
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
