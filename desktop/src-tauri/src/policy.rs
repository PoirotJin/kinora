//! Selected cloud origin and the method + path allow-list for the local UI bridge.
//! Admin, production and storage operations stay in the web workbench.
use serde_json::{Map, Value};
use url::Url;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Json,
    Binary,
}

#[derive(Debug, PartialEq, Eq)]
pub struct Resolved {
    pub url: String,
    pub post: bool,
    pub kind: Kind,
}

const JSON_GET: &[&str] = &[
    "/api/gadget/keys", "/api/auth/me", "/api/topics", "/api/tags", "/api/library/find", "/api/library/search",
    "/api/library/collections", "/api/personal/motions", "/api/custom", "/api/custom/results", "/api/rewrite/models", "/api/retarget",
    "/api/retarget/model", "/api/retarget/mesh-preview",
];
const BINARY_GET: &[&str] = &["/api/retarget/mesh-data"];
const JSON_POST: &[&str] = &[
    "/api/library/collections",
    "/api/gadget/keys", "/api/gadget/keys/revoke", "/api/auth/login", "/api/auth/logout", "/api/auth/password",
    "/api/custom", "/api/custom/cancel", "/api/retarget", "/api/retarget/cancel",
];
const DOWNLOADS: &[&str] = &["/api/retarget/download", "/api/retarget/repair-download", "/gadget/agent-kit.zip"];

fn repair_import(path: &str) -> bool {
    path.strip_prefix("/api/harness/robot-edits/")
        .and_then(|rest| rest.strip_suffix("/import-repair"))
        .is_some_and(|id| id.len() == 32 && id.bytes().all(|b| b.is_ascii_hexdigit()))
}

pub fn request_body_limit(path: &str) -> usize {
    if path == "/api/harness/robots" || path == "/api/harness/video-motion" {
        45 * 1024 * 1024
    } else if path == "/api/harness/multi-scene" {
        128 * 1024
    } else if matches!(path, "/api/harness/personal-assets/inspect-bvh" | "/api/harness/personal-assets/import-bvh" | "/api/harness/personal-assets/rigs" | "/api/harness/personal-assets/objects") {
        12 * 1024 * 1024
    } else if path == "/api/harness/scene" || path == "/api/custom" {
        96 * 1024
    } else if repair_import(path) {
        17 * 1024 * 1024
    } else {
        64 * 1024
    }
}

pub fn same_origin(value: &str, origin: &str) -> bool {
    match Url::parse(value) {
        Ok(url) => (url.scheme() == "https" || (url.scheme() == "http" && crate::server::loopback(&url))) && url.username().is_empty() && url.password().is_none() && url.origin().ascii_serialization() == origin,
        Err(_) => false,
    }
}

fn motion_id(value: &str) -> bool {
    value.len() >= 9 && value.starts_with('m') && value[1..].bytes().all(|b| b.is_ascii_digit() || b.is_ascii_lowercase())
}

/// `/api/motions/{id}`, `/thumbnail` (JSON) and `/preview.bin` (binary).
fn motion_rule(path: &str) -> Option<Kind> {
    let rest = path.strip_prefix("/api/motions/")?;
    let (id, tail) = rest.split_once('/').map_or((rest, ""), |(a, b)| (a, b));
    if !motion_id(id) {
        return None;
    }
    match tail {
        "" | "thumbnail" if !rest.ends_with('/') => Some(Kind::Json),
        "preview.bin" => Some(Kind::Binary),
        _ => None,
    }
}

fn asset_id(id: &str) -> bool {
    id.len() == 32 && id.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn asset_rule(post: bool, path: &str) -> Option<Kind> {
    if ["rigs", "poses", "objects", "snapshots"].contains(&path)
        || post && ["inspect-bvh", "import-bvh", "apply-pose"].contains(&path)
        || !post && ["motions", "history", "snapshot-source"].contains(&path)
    {
        return Some(Kind::Json);
    }
    let (group, rest) = path.split_once('/')?;
    let (id, tail) = rest.split_once('/').unwrap_or((rest, ""));
    (!post && asset_id(id) && (["rigs", "poses", "objects", "snapshots", "snapshot-jobs"].contains(&group) && tail.is_empty() && !rest.ends_with('/')
        || group == "objects" && tail == "mesh" || group == "rigs" && tail == "calibration")).then_some(Kind::Json)
}

fn harness_rule(post: bool, path: &str) -> Option<Kind> {
    let rest = path.strip_prefix("/api/harness/")?;
    if let Some(asset) = rest.strip_prefix("personal-assets/") { return asset_rule(post, asset); }
    if (!post && ["capabilities", "results"].contains(&rest)) || rest == "tasks" { return Some(Kind::Json); }
    if rest == "video-motion" { return Some(Kind::Json); }
    if let Some(id) = rest.strip_prefix("video-motion/") {
        return (!post && id.len() == 64 && id.bytes().all(|b| b.is_ascii_hexdigit())).then_some(Kind::Json);
    }
    if rest == "robots" { return Some(Kind::Json); }
    if let Some(rest) = rest.strip_prefix("robots/") {
        let (id, tail) = rest.split_once('/')?;
        return (!post && tail == "preview" && id.starts_with("robot_") && id.len() == 38 && id[6..].bytes().all(|b| b.is_ascii_hexdigit())).then_some(Kind::Json);
    }
    if post && ["splice", "splice-preview", "robot-edits", "scene", "paired-scene", "multi-scene", "object-response"].contains(&rest) { return Some(Kind::Json); }
    if let Some(rest) = rest.strip_prefix("motions/") {
        let (id, tail) = rest.split_once('/')?;
        return (!post && !id.is_empty() && id.len() <= 96 && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-') && tail == "preview").then_some(Kind::Json);
    }
    if let Some(rest) = rest.strip_prefix("robot-edits/") {
        let (id, action) = rest.split_once('/').unwrap_or((rest, ""));
        return (id.len()==32 && id.bytes().all(|b| b.is_ascii_hexdigit()) && ((!post && ["", "preview", "model"].contains(&action)) || (post && ["edit", "history"].contains(&action)))).then_some(Kind::Json);
    }
    let rest = rest.strip_prefix("tasks/")?;
    let (id, action) = rest.split_once('/').unwrap_or((rest, ""));
    if id.is_empty() || id.len() > 100 || !id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-') { return None; }
    ((!post && (action.is_empty() || action == "paired-preview" || action == "multi-preview")) || (post && ["repair", "repair-status", "accept", "scene-status", "scene-resume", "cancel"].contains(&action))).then_some(Kind::Json)
}

fn rule(post: bool, path: &str) -> Option<Kind> {
    if post && repair_import(path) { return Some(Kind::Json); }
    if let Some(kind) = harness_rule(post, path) { return Some(kind); }
    if post {
        return JSON_POST.contains(&path).then_some(Kind::Json);
    }
    if JSON_GET.contains(&path) {
        return Some(Kind::Json);
    }
    if BINARY_GET.contains(&path) {
        return Some(Kind::Binary);
    }
    motion_rule(path)
}

/// Query values must be scalars; keys are short lowercase identifiers.
fn scalar_query_with(query: Option<&Value>, max_ids_chars: Option<usize>) -> Result<String, String> {
    let map: &Map<String, Value> = match query {
        None | Some(Value::Null) => return Ok(String::new()),
        Some(Value::Object(map)) => map,
        _ => return Err("invalid query".into()),
    };
    let mut out = url::form_urlencoded::Serializer::new(String::new());
    let mut any = false;
    for (key, value) in map {
        if key.is_empty() || key.len() > 32 || !key.bytes().all(|b| b.is_ascii_lowercase() || b == b'_') {
            return Err("invalid query key".into());
        }
        let text = match value {
            Value::Null => continue,
            Value::String(s) => s.clone(),
            Value::Number(n) => n.to_string(),
            Value::Bool(b) => b.to_string(),
            _ => return Err("invalid query value".into()),
        };
        if text.is_empty() {
            continue;
        }
        let max_len = if key == "ids" { max_ids_chars.unwrap_or(500) } else { 500 };
        if text.chars().count() > max_len {
            return Err("invalid query value".into());
        }
        out.append_pair(key, &text);
        any = true;
    }
    Ok(if any { format!("?{}", out.finish()) } else { String::new() })
}

pub fn scalar_query(query: Option<&Value>) -> Result<String, String> {
    scalar_query_with(query, None)
}

fn build_with_query_limit(path: &str, query: Option<&Value>, origin: &str, max_ids_chars: Option<usize>) -> Result<String, String> {
    if path.len() > 200 {
        return Err("invalid path".into());
    }
    let query_string = match max_ids_chars {
        Some(limit) => scalar_query_with(query, Some(limit))?,
        None => scalar_query(query)?,
    };
    let url = format!("{origin}{path}{query_string}");
    let parsed = Url::parse(&url).map_err(|_| "invalid path".to_string())?;
    if !same_origin(&url, origin) || parsed.path() != path || parsed.fragment().is_some() {
        return Err("invalid path".into());
    }
    Ok(url)
}

fn build(path: &str, query: Option<&Value>, origin: &str) -> Result<String, String> {
    build_with_query_limit(path, query, origin, None)
}

pub fn resolve(method: Option<&str>, path: &str, query: Option<&Value>, origin: &str) -> Result<Resolved, String> {
    let post = method == Some("POST");
    let kind = rule(post, path).ok_or_else(|| "endpoint not allowed".to_string())?;
    Ok(Resolved { url: build(path, query, origin)?, post, kind })
}

pub fn resolve_download(path: &str, query: Option<&Value>, origin: &str) -> Result<String, String> {
    if path == "/gadget/agent-kit.zip" && query.is_some_and(|v| !v.is_null() && !v.as_object().is_some_and(|m|m.is_empty())) { return Err("gadget download takes no query".into()); }
    let robot_download = path.strip_prefix("/api/harness/robot-edits/").and_then(|v|v.strip_suffix("/download")).is_some_and(|id|id.len()==32 && id.bytes().all(|b|b.is_ascii_hexdigit()));
    if robot_download {
        let map = query.and_then(Value::as_object).ok_or("missing version")?;
        if map.len()!=1 || !map.get("version").and_then(Value::as_str).is_some_and(|v|v.len()==32 && v.bytes().all(|b|b.is_ascii_hexdigit())) {return Err("invalid version".into());}
    }
    if path == "/api/retarget/repair-download" {
        let map = query.and_then(Value::as_object).ok_or("missing job ID")?;
        if map.len()!=1 || !map.get("id").and_then(Value::as_str).is_some_and(|id|id.len()==32 && id.bytes().all(|b|b.is_ascii_hexdigit())) {
            return Err("invalid job ID".into());
        }
    }
    let motion_download = path.strip_prefix("/api/harness/motions/")
        .and_then(|rest| rest.strip_suffix("/download"))
        .is_some_and(|id| !id.is_empty() && id.len() <= 96 && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-'));
    let batch = path == "/api/harness/export";
    if motion_download || batch {
        let empty = Map::new();
        let map = match query { None | Some(Value::Null) => &empty, Some(Value::Object(m)) => m, _ => return Err("invalid export query".into()) };
        if map.keys().any(|k| k != "format" && !(batch && k == "ids")) { return Err("invalid export query".into()); }
        if map.get("format").is_some_and(|v| !matches!(v.as_str(), Some("npz" | "bvh"))) { return Err("invalid export format".into()); }
        if batch {
            let ids = map.get("ids").and_then(Value::as_str).ok_or("missing motion IDs")?;
            let parts: Vec<_> = ids.split(',').collect();
            if parts.is_empty() || parts.len() > 20 || parts.iter().any(|id| id.is_empty() || id.len()>96 || !id.bytes().all(|b| b.is_ascii_alphanumeric() || b==b'_' || b==b'-')) { return Err("invalid motion IDs".into()); }
        }
    }
    let snapshot = path.strip_prefix("/api/harness/personal-assets/snapshots/")
        .and_then(|v| v.strip_suffix("/download")).is_some_and(asset_id);
    if snapshot {
        let map = query.and_then(Value::as_object).ok_or("missing snapshot batch")?;
        if map.len() != 1 || !map.get("batch").and_then(Value::as_u64).is_some_and(|n| (1..=1000).contains(&n)) {
            return Err("invalid snapshot batch".into());
        }
    }
    if !DOWNLOADS.contains(&path) && !motion_download && !batch && !robot_download && !snapshot {
        return Err("download not allowed".into());
    }
    let max_ids_chars = matches!(path, "/api/harness/export" | "/api/library/export").then_some(2048);
    build_with_query_limit(path, query, origin, max_ids_chars)
}

pub fn safe_file_name(value: Option<&str>) -> String {
    let cleaned: String = value
        .unwrap_or("")
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-') { c } else { '_' })
        .collect();
    let trimmed = cleaned.trim_start_matches('.');
    let name: String = trimmed.chars().take(120).collect();
    if name.is_empty() { "motionfarm.npz".into() } else { name }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    const O: &str = "https://your-kinora.example.com";

    #[test]
    fn personal_assets_are_owner_route_shapes_with_batched_zip_downloads() {
        let base = "/api/harness/personal-assets";
        let id = "a".repeat(32);
        let snapshots = format!("{base}/snapshots");
        let download = format!("{snapshots}/{id}/download");
        assert!(resolve(Some("POST"), &snapshots, None, O).is_ok());
        assert!(resolve(None, &format!("{base}/snapshot-jobs/{id}"), None, O).is_ok());
        assert!(resolve(None, &format!("{base}/rigs/{id}/calibration"), None, O).is_ok());
        assert!(resolve(None, &format!("{base}/inspect-bvh"), None, O).is_err());
        assert!(resolve(None, &format!("{base}/objects/../../mesh"), None, O).is_err());
        assert!(resolve(None, &download, None, O).is_err());
        assert!(resolve_download(&download, Some(&json!({"batch":1})), O).is_ok());
        assert!(resolve_download(&download, Some(&json!({"batch":0})), O).is_err());
        assert!(resolve_download(&download, Some(&json!({"batch":1,"owner":"other"})), O).is_err());
    }

    #[test]
    fn robot_editor_routes_and_version_download_are_bounded() {
        let id="a".repeat(32);let base=format!("/api/harness/robot-edits/{id}");
        assert!(resolve(Some("POST"),"/api/harness/robot-edits",None,O).is_ok());
        assert!(resolve(None,&format!("{base}/preview"),None,O).is_ok());
        assert!(resolve(Some("POST"),&format!("{base}/edit"),None,O).is_ok());
        assert!(resolve(None,&format!("{base}/edit"),None,O).is_err());
        assert!(resolve(None,"/api/harness/robot-edits/../../admin",None,O).is_err());
        assert!(resolve_download(&format!("{base}/download"),Some(&json!({"version":id})),O).is_ok());
        assert!(resolve_download(&format!("{base}/download"),None,O).is_err());
        assert!(resolve_download(&format!("{base}/download"),Some(&json!({"version":"original"})),O).is_err());
    }
    #[test]
    fn navigation_cannot_escape_https_origin() {
        let origin = "https://example.com";
        assert!(same_origin("https://example.com/api/motions", origin));
        for value in ["https://example.com.evil.com", "https://example.com@evil.com", "http://example.com", "javascript:alert(1)", "https://example.com:444"] {
            assert!(!same_origin(value, origin), "{value}");
        }
    }

    #[test]
    fn selected_https_origin_remains_compatible() {
        assert!(same_origin(&format!("{O}/api/auth/me"), O));
    }

    #[test]
    fn selected_loopback_origin_keeps_the_endpoint_and_origin_boundary() {
        let origin = "http://127.0.0.1:8765";
        assert!(resolve(None, "/api/auth/me", None, origin).is_ok());
        assert!(!same_origin("http://127.0.0.1:9000/api/auth/me", origin));
        assert!(!same_origin("http://example.com/api/auth/me", "http://example.com"));
        assert!(resolve(None, "//example.com/api/auth/me", None, origin).is_err());
        assert!(!same_origin("https://user:pass@example.com", "https://example.com"));
    }

    #[test]
    fn bridge_allows_only_listed_endpoints() {
        assert_eq!(resolve(None, "/api/auth/me", None, O).unwrap(), Resolved { url: format!("{O}/api/auth/me"), post: false, kind: Kind::Json });
        assert_eq!(resolve(None, "/api/motions/m00000001/preview.bin", None, O).unwrap().kind, Kind::Binary);
        assert_eq!(resolve(None, "/api/motions/m00000001/thumbnail", None, O).unwrap().kind, Kind::Json);
        assert_eq!(resolve(None, "/api/retarget/mesh-data", None, O).unwrap().kind, Kind::Binary);
        assert!(resolve(Some("POST"), "/api/custom", None, O).unwrap().post);
        assert_eq!(resolve(None, "/api/library/find", Some(&json!({"q": "转弯", "sort": "duration_desc", "topic": ""})), O).unwrap().url,
                   format!("{O}/api/library/find?q=%E8%BD%AC%E5%BC%AF&sort=duration_desc"));
        for (method, path) in [
            (None, "/api/users"), (Some("POST"), "/api/production/start"), (Some("POST"), "/api/auth/me"),
            (None, "/api/custom/../users"), (None, "/api/motions/m1/preview.bin"), (None, "//evil.com/api/auth/me"),
            (None, "/api/auth/me?x=1"), (None, "/api/auth/me#x"), (None, "/api/storage/download"),
            (None, "https://evil.com/api/auth/me"), (Some("POST"), "/api/motions/m00000001/review"),
            (None, "/api/retarget/download"), (None, "/api/motions/m00000001/"), (None, "/api/motions/m00000001/x"),
            (None, "/api/motions/M00000001"),
        ] {
            assert!(resolve(method, path, None, O).is_err(), "{method:?} {path}");
        }
    }

    #[test]
    fn custom_robot_endpoints_are_scoped() {
        assert!(resolve(Some("POST"), "/api/harness/robots", None, O).is_ok());
        assert!(resolve(None, "/api/harness/robots", None, O).is_ok());
        let path = "/api/harness/robots/robot_0123456789abcdef0123456789abcdef/preview";
        assert!(resolve(None, path, None, O).is_ok());
        assert!(resolve(Some("POST"), path, None, O).is_err());
        assert!(resolve(None, "/api/harness/robots/../../secret/preview", None, O).is_err());
        assert!(resolve(None, "/api/harness/robots/robot_bad/preview", None, O).is_err());
    }

    #[test]
    fn correction_endpoints_are_scoped() {
        for (method, path) in [(None, "/api/harness/capabilities"), (None, "/api/harness/tasks"), (Some("POST"), "/api/harness/tasks"), (None, "/api/harness/tasks/task_123"), (Some("POST"), "/api/harness/tasks/task_123/repair"), (Some("POST"), "/api/harness/tasks/task_123/repair-status"), (Some("POST"), "/api/harness/tasks/task_123/accept"), (Some("POST"), "/api/harness/tasks/task_123/cancel"), (Some("POST"), "/api/harness/splice"), (None, "/api/harness/motions/m00000001/preview"), (None, "/api/harness/motions/edit_ab123/preview")] {
            assert!(resolve(method, path, None, O).is_ok(), "{path}");
        }
        for (method, path) in [(Some("POST"), "/api/harness/capabilities"), (None, "/api/harness/splice"), (Some("POST"), "/api/harness/splice/x"), (Some("POST"), "/api/harness/tasks/t/advance"), (None, "/api/harness/tasks/t/repair"), (Some("POST"), "/api/harness/motions/m00000001/preview"), (None, "/api/harness/tasks/../users")] {
            assert!(resolve(method, path, None, O).is_err(), "{path}");
        }
    }

    #[test]
    fn scene_requests_use_dedicated_routes_and_bounds() {
        let origin = "https://farm.example";
        let scene = "/api/harness/scene";
        assert!(resolve(Some("POST"), scene, None, origin).is_ok());
        assert!(resolve(None, scene, None, origin).is_err());
        assert_eq!(request_body_limit(scene), 96 * 1024);
        let status = "/api/harness/tasks/task_123/scene-status";
        assert!(resolve(Some("POST"), status, None, origin).is_ok());
        assert_eq!(request_body_limit(status), 64 * 1024);
        let resume = "/api/harness/tasks/task_123/scene-resume";
        assert!(resolve(Some("POST"), resume, None, origin).is_ok());
        assert!(resolve(None, resume, None, origin).is_err());
        assert!(resolve(Some("POST"), "/api/harness/tasks/task_123/advance", None, origin).is_err());
    }

    #[test]
    fn multi_scene_and_object_response_routes_and_bounds() {
        let origin = "https://farm.example";
        let multi = "/api/harness/multi-scene";
        assert!(resolve(Some("POST"), multi, None, origin).is_ok());
        assert!(resolve(None, multi, None, origin).is_err());
        assert_eq!(request_body_limit(multi), 128 * 1024);

        let multi_preview = "/api/harness/tasks/task_123/multi-preview";
        assert!(resolve(None, multi_preview, None, origin).is_ok());
        assert!(resolve(Some("POST"), multi_preview, None, origin).is_err());
        assert!(resolve(None, "/api/harness/tasks/../multi-preview", None, origin).is_err());

        let obj_resp = "/api/harness/object-response";
        assert!(resolve(Some("POST"), obj_resp, None, origin).is_ok());
        assert!(resolve(None, obj_resp, None, origin).is_err());
        assert_eq!(request_body_limit(obj_resp), 64 * 1024);
    }

    #[test]
    fn query_values_are_scalar() {
        assert!(scalar_query(Some(&json!({"id": ["a", "b"]}))).is_err());
        assert!(scalar_query(Some(&json!({"a&b": "1"}))).is_err());
        assert!(scalar_query(Some(&json!("history=1"))).is_err());
        assert!(scalar_query(Some(&json!({"q": "x".repeat(501)}))).is_err());
        assert_eq!(scalar_query(Some(&json!({"id": "a&b=c"}))).unwrap(), "?id=a%26b%3Dc");
        assert_eq!(scalar_query(Some(&json!({"after": 0, "history": true, "x": null}))).unwrap(), "?after=0&history=true");
    }

    #[test]
    fn gadget_owner_endpoints_and_fixed_package_only() {
        assert!(resolve(Some("GET"), "/api/gadget/keys", None, O).is_ok());
        assert!(resolve(Some("POST"), "/api/gadget/keys", None, O).is_ok());
        assert!(resolve(Some("POST"), "/api/gadget/keys/revoke", None, O).is_ok());
        assert!(resolve(Some("GET"), "/api/gadget/keys/revoke", None, O).is_err());
        assert!(resolve(Some("POST"), "/api/users", None, O).is_err());
        assert!(resolve_download("/gadget/agent-kit.zip", None, O).is_ok());
        assert!(resolve_download("/gadget/agent-kit.zip", Some(&json!({"url":"https://evil.test"})), O).is_err());
        assert!(resolve_download("/gadget/other.zip", None, O).is_err());
    }

    #[test]
    fn downloads_are_limited_with_safe_names() {
        assert_eq!(resolve_download("/api/retarget/download", Some(&json!({"id": "abc"})), O).unwrap(), format!("{O}/api/retarget/download?id=abc"));
        assert!(resolve_download("/api/storage/download", None, O).is_err());
        assert!(resolve_download("/api/harness/motions/m00000001/download", None, O).is_ok());
        assert!(resolve_download("/api/harness/motions/ma_abc/download", None, O).is_ok());
        for path in ["/api/harness/motions/../download", "/api/harness/motions/a/b/download", "/api/harness/motions//download", "/api/harness/motions/ma_a/download/extra"] {
            assert!(resolve_download(path, None, O).is_err());
        }
        assert!(resolve_download("/api/harness/motions/ma_a/download", Some(&json!({"owner":"other"})), O).is_err());
        assert_eq!(safe_file_name(Some("../../etc/passwd")), "_.._etc_passwd");
        assert_eq!(safe_file_name(Some("m00000001_unitree_g1_23dof_reference.npz")), "m00000001_unitree_g1_23dof_reference.npz");
        assert_eq!(safe_file_name(Some("")), "motionfarm.npz");
        assert_eq!(safe_file_name(None), "motionfarm.npz");
    }
    #[test]
    fn batch_export_allows_twenty_managed_and_imported_ids_within_2048_chars() {
        let ma_ids = (0..20).map(|i| format!("ma_{i:032x}")).collect::<Vec<_>>().join(",");
        assert!(ma_ids.len() > 500 && ma_ids.len() <= 2048);
        assert!(resolve_download("/api/harness/export", Some(&json!({"ids": ma_ids, "format": "npz"})), O).is_ok());

        let mimp_ids = (0..20).map(|i| format!("mimp{i:032x}")).collect::<Vec<_>>().join(",");
        assert!(mimp_ids.len() > 500 && mimp_ids.len() <= 2048);
        assert!(resolve_download("/api/harness/export", Some(&json!({"ids": mimp_ids, "format": "bvh"})), O).is_ok());

        assert!(scalar_query(Some(&json!({"ids": ma_ids}))).is_err());
        assert_eq!(request_body_limit("/api/custom"), 96 * 1024);
    }

    #[test]
    fn repair_uses_only_the_dedicated_upload_and_download_paths() {
        let id = "a".repeat(32);
        let upload = format!("/api/harness/robot-edits/{id}/import-repair");
        let download = "/api/retarget/repair-download";
        assert!(resolve(Some("POST"), &upload, None, O).is_ok());
        assert!(resolve(None, &upload, None, O).is_err());
        assert!(resolve(Some("POST"), "/api/harness/robot-edits/abc/import-repair", None, O).is_err());
        assert_eq!(request_body_limit(&upload), 17 * 1024 * 1024);
        assert_eq!(request_body_limit(&upload.replace("import-repair", "edit")), 64 * 1024);
        assert!(resolve_download(download, Some(&json!({"id": id})), O).is_ok());
        assert!(resolve_download(download, Some(&json!({"id": "abc"})), O).is_err());
        assert!(resolve_download(download, Some(&json!({"id": id, "unsafe": 1})), O).is_err());
    }
}
