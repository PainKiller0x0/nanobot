pub const PROFILE_UPDATE_PROMPT: &str = "你是一个事实提取专家。请分析以下对话，提取关于用户的偏好、习惯、事实。输出格式为简洁的 Markdown 列表。";

pub const PLAYBOOK_EXTRACTION_PROMPT: &str = "你是一个行为模式分析专家。请分析以下对话，如果用户对某个任务有特定的操作偏好或流程要求，请将其总结为可复用的 'Playbook'。";

pub struct ReasoningEngine;

impl ReasoningEngine {
    pub fn build_reflection_payload(interaction: &str) -> String {
        format!(
            "Context:\n{}\n\nTask: 分析以上交互并提取核心事实。",
            interaction
        )
    }
}
