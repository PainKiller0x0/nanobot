use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeSet;
use std::fs;
use std::path::Path;

#[derive(Debug, Serialize, Deserialize, Clone)]
#[serde(default)]
pub struct Channel {
    pub id: Option<u64>,
    pub name: String,
    pub r#type: String,
    pub key: String,
    pub base: String,
    pub models: String,
    pub model_mapping: String,
    pub status: String,
    pub requests: u64,
    pub last_test: Option<String>,
    pub fail_count: u32,
    pub role: String,
    pub group: String,
    pub priority: u32,
    pub cost_model: String,
}

impl Default for Channel {
    fn default() -> Self {
        Self {
            id: None,
            name: String::new(),
            r#type: "openai".to_string(),
            key: String::new(),
            base: String::new(),
            models: "*".to_string(),
            model_mapping: String::new(),
            status: "active".to_string(),
            requests: 0,
            last_test: None,
            fail_count: 0,
            role: "default".to_string(),
            group: String::new(),
            priority: 100,
            cost_model: String::new(),
        }
    }
}

impl Channel {
    pub fn is_active(&self) -> bool {
        self.status.trim().is_empty() || self.status.eq_ignore_ascii_case("active")
    }

    pub fn role_key(&self) -> String {
        let role = self.role.trim();
        if role.is_empty() {
            "default".to_string()
        } else {
            role.to_lowercase()
        }
    }

    pub fn group_key(&self) -> String {
        self.group.trim().to_lowercase()
    }

    pub fn supports_model(&self, model: &str) -> bool {
        let models = self.models.trim();
        if models.is_empty() || models == "*" {
            return true;
        }
        let target = model.trim().to_lowercase();
        self.model_set().contains(&target)
    }

    pub fn mapped_model(&self, requested_model: &str, desired_model: &str) -> String {
        let mapping = self.mapping_value();
        if let Some(mapped) = lookup_mapping(mapping.as_ref(), desired_model) {
            return mapped;
        }
        if let Some(mapped) = lookup_mapping(mapping.as_ref(), requested_model) {
            return mapped;
        }
        if !desired_model.trim().is_empty() && self.supports_model(desired_model) {
            return desired_model.to_string();
        }
        if !requested_model.trim().is_empty() && self.supports_model(requested_model) {
            return requested_model.to_string();
        }
        self.first_model()
            .unwrap_or_else(|| desired_model.to_string())
    }

    fn model_set(&self) -> BTreeSet<String> {
        self.models
            .split(',')
            .map(|item| item.trim().to_lowercase())
            .filter(|item| !item.is_empty())
            .collect()
    }

    fn first_model(&self) -> Option<String> {
        self.models
            .split(',')
            .map(str::trim)
            .find(|item| !item.is_empty() && *item != "*")
            .map(ToString::to_string)
    }

    fn mapping_value(&self) -> Option<Value> {
        if self.model_mapping.trim().is_empty() {
            return None;
        }
        serde_json::from_str::<Value>(&self.model_mapping).ok()
    }
}

fn lookup_mapping(mapping: Option<&Value>, model: &str) -> Option<String> {
    let map = mapping?.as_object()?;
    let exact = map.get(model).and_then(Value::as_str);
    if let Some(value) = exact {
        return Some(value.to_string());
    }
    let target = model.to_lowercase();
    map.iter()
        .find(|(key, _)| key.to_lowercase() == target)
        .and_then(|(_, value)| value.as_str())
        .map(ToString::to_string)
}

#[derive(Debug, Serialize, Deserialize, Clone)]
#[serde(default)]
pub struct RouterConfig {
    pub enabled: bool,
    pub dry_run: bool,
    pub external_enabled: bool,
    pub external_allowed_models: Vec<String>,
    pub default_model: String,
    pub pro_model: String,
    pub emergency_model: String,
    pub backup_model: String,
    pub default_group: String,
    pub pro_group: String,
    pub emergency_group: String,
    pub backup_group: String,
    pub pro_prompt_chars: usize,
    pub pro_message_count: usize,
    pub monthly_warn_rmb: f64,
    pub monthly_downgrade_rmb: f64,
    pub monthly_hard_limit_rmb: f64,
    pub retry_statuses: Vec<u16>,
    pub pro_keywords: Vec<String>,
}

impl Default for RouterConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            dry_run: false,
            external_enabled: true,
            external_allowed_models: vec![
                "deepseek-v4-flash".to_string(),
                "deepseek-v4-pro".to_string(),
                "MiniMax-M2.7".to_string(),
                "LongCat-Flash-Chat".to_string(),
            ],
            default_model: "deepseek-v4-flash".to_string(),
            pro_model: "deepseek-v4-pro".to_string(),
            emergency_model: "LongCat-Flash-Chat".to_string(),
            backup_model: "coding-plan".to_string(),
            default_group: "deepseek".to_string(),
            pro_group: "deepseek".to_string(),
            emergency_group: "longcat".to_string(),
            backup_group: String::new(),
            pro_prompt_chars: 18_000,
            pro_message_count: 14,
            monthly_warn_rmb: 10.0,
            monthly_downgrade_rmb: 20.0,
            monthly_hard_limit_rmb: 30.0,
            retry_statuses: vec![408, 409, 425, 429, 500, 502, 503, 504, 529],
            pro_keywords: vec![
                "深度".to_string(),
                "架构".to_string(),
                "review".to_string(),
                "重构".to_string(),
                "复杂".to_string(),
                "取舍".to_string(),
                "方案".to_string(),
                "设计".to_string(),
                "迁移".to_string(),
                "推理".to_string(),
                "长文".to_string(),
                "全盘".to_string(),
            ],
        }
    }
}

pub fn load_config<P: AsRef<Path>>(path: P) -> Vec<Channel> {
    if !path.as_ref().exists() {
        return Vec::new();
    }
    let data = fs::read_to_string(path).unwrap_or_default();
    let value = serde_json::from_str::<Value>(&data).unwrap_or(Value::Null);
    if let Ok(channels) = serde_json::from_value::<Vec<Channel>>(value.clone()) {
        return channels;
    }
    value
        .get("channels")
        .cloned()
        .and_then(|v| serde_json::from_value::<Vec<Channel>>(v).ok())
        .unwrap_or_default()
}

pub fn save_config<P: AsRef<Path>>(path: P, channels: &[Channel]) {
    if let Some(parent) = path.as_ref().parent() {
        let _ = fs::create_dir_all(parent);
    }
    let data = serde_json::to_string_pretty(channels).unwrap_or_default();
    let _ = fs::write(path, data);
}

pub fn load_router_config<P: AsRef<Path>>(path: P) -> RouterConfig {
    if !path.as_ref().exists() {
        return RouterConfig::default();
    }
    let data = fs::read_to_string(path).unwrap_or_default();
    serde_json::from_str(&data).unwrap_or_default()
}

pub fn save_router_config<P: AsRef<Path>>(path: P, router: &RouterConfig) {
    if let Some(parent) = path.as_ref().parent() {
        let _ = fs::create_dir_all(parent);
    }
    let data = serde_json::to_string_pretty(router).unwrap_or_default();
    let _ = fs::write(path, data);
}
