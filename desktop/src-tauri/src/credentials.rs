//! Credentials are stored only in the OS vault, never in application files.
use serde::{Deserialize, Serialize};
#[derive(Serialize, Deserialize)]
pub struct Credentials { pub username: String, pub password: String }
fn service_name(identifier: &str, origin: &str) -> String {
    format!("{identifier}:{origin}")
}
fn entry(identifier: &str, origin: &str) -> Result<keyring::Entry, String> {
    keyring::Entry::new(&service_name(identifier, origin), "saved-login")
        .map_err(|_| "无法访问系统凭据库".into())
}
#[tauri::command]
pub async fn load_credentials(app: tauri::AppHandle, state: tauri::State<'_, crate::Cloud>, server: String) -> Result<Option<Credentials>, String> {
    let origin = state.current_for(&server)?.origin.clone();
    let identifier = app.config().identifier.clone();
    tauri::async_runtime::spawn_blocking(move || match entry(&identifier, &origin)?.get_password() {
        Ok(value) => serde_json::from_str(&value).map(Some).map_err(|_| "已保存的凭据无法读取".into()),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(_) => Err("无法读取系统凭据库，请解锁后重试".into()),
    }).await.map_err(|_| "无法读取系统凭据库")?
}
#[tauri::command]
pub async fn save_credentials(app: tauri::AppHandle, state: tauri::State<'_, crate::Cloud>, server: String, credentials: Credentials) -> Result<(), String> {
    if credentials.username.is_empty() || credentials.username.len() > 1024 || credentials.password.len() > 4096 {
        return Err("凭据格式无效".into());
    }
    let origin = state.current_for(&server)?.origin.clone();
    let identifier = app.config().identifier.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let value = serde_json::to_string(&credentials).map_err(|_| "凭据格式无效")?;
        entry(&identifier, &origin)?.set_password(&value).map_err(|_| "无法保存到系统凭据库".into())
    }).await.map_err(|_| "无法保存到系统凭据库")?
}
#[tauri::command]
pub async fn forget_credentials(app: tauri::AppHandle, state: tauri::State<'_, crate::Cloud>, server: String) -> Result<(), String> {
    let origin = state.current_for(&server)?.origin.clone();
    let identifier = app.config().identifier.clone();
    tauri::async_runtime::spawn_blocking(move || match entry(&identifier, &origin)?.delete_credential() {
        Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
        Err(_) => Err("无法删除系统中保存的凭据".into()),
    }).await.map_err(|_| "无法删除系统中保存的凭据")?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn credential_namespace_preserves_release_and_isolates_test_apps() {
        let origin = "https://server.example";
        let release = service_name("com.motionfarm.desktop", origin);
        assert_eq!(release, "com.motionfarm.desktop:https://server.example");
        let test_a = service_name("com.motionfarm.desktop.test-a", origin);
        let test_b = service_name("com.motionfarm.desktop.test-b", origin);
        assert_ne!(release, test_a);
        assert_ne!(test_a, test_b);
        assert_ne!(test_a, service_name("com.motionfarm.desktop.test-a", "https://other.example"));
    }
}
