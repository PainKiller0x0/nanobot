use crate::config::Channel;
use axum::{
    body::Body,
    extract::State,
    http::{Request, Response, StatusCode},
    response::IntoResponse,
};
use futures_util::StreamExt;
use reqwest::{Body as ReqBody, Client};
use std::sync::Arc;
use tokio::sync::Mutex;

pub struct ProxyState {
    pub client: Client,
    pub channels: Mutex<Vec<Channel>>,
    pub index: Mutex<usize>,
}

pub async fn handle_proxy(
    State(state): State<Arc<ProxyState>>,
    req: Request<Body>,
) -> impl IntoResponse {
    let mut channels = state.channels.lock().await;
    if channels.is_empty() {
        return (StatusCode::NOT_FOUND, "No channels available").into_response();
    }

    let mut idx = state.index.lock().await;
    let ch = channels[*idx % channels.len()].clone();
    *idx += 1;
    drop(channels);

    let target_url = format!("{}/v1/chat/completions", ch.base.trim_end_matches('/'));
    let (parts, body) = req.into_parts();

    // 将 Axum Body 转为 Reqwest Body
    let stream = body.into_data_stream();
    let req_body = ReqBody::wrap_stream(stream);

    let mut target_req = state
        .client
        .post(&target_url)
        .header("Authorization", format!("Bearer {}", ch.key))
        .body(req_body);

    for (name, value) in parts.headers.iter() {
        if name != "host" && name != "authorization" {
            target_req = target_req.header(name, value);
        }
    }

    let response = match target_req.send().await {
        Ok(res) => res,
        Err(e) => {
            return (StatusCode::BAD_GATEWAY, format!("Upstream error: {}", e)).into_response()
        }
    };

    let mut res_builder = Response::builder().status(
        StatusCode::from_u16(response.status().as_u16())
            .unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
    );

    for (name, value) in response.headers().iter() {
        res_builder = res_builder.header(name, value);
    }

    let res_stream = response.bytes_stream();
    res_builder
        .body(Body::from_stream(res_stream))
        .unwrap_or_else(|_| (StatusCode::INTERNAL_SERVER_ERROR, "Internal Error").into_response())
}
