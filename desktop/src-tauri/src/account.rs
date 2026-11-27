//! Only fixed account routes on the selected server may leave the application.
pub fn page_url(origin: &str, mode: &str) -> Result<String, String> {
    if !matches!(mode, "register" | "forgot") {
        return Err("无效的账号入口".into());
    }
    let origin = crate::server::normalize(origin)?;
    Ok(format!("{origin}/account?view={mode}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn account_routes_follow_the_selected_server() {
        for mode in ["register", "forgot"] {
            for origin in ["https://example.com:8765", "http://localhost:8765", "http://127.0.0.1:8765"] {
                assert_eq!(page_url(origin, mode).unwrap(), format!("{origin}/account?view={mode}"));
            }
        }
    }

    #[test]
    fn rejects_arbitrary_targets_and_modes() {
        for origin in ["file:///tmp/test", "javascript:alert(1)", "https://user:pass@example.com", "https://example.com/path", "https://example.com?redirect=evil", "http://example.com"] {
            assert!(page_url(origin, "register").is_err(), "{origin}");
        }
        for mode in ["", "login", "register&redirect=https://evil.example", "https://evil.example"] {
            assert!(page_url("https://example.com", mode).is_err(), "{mode}");
        }
    }
}
