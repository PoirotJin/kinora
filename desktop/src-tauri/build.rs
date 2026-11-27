fn main() {
    println!("cargo:rerun-if-changed=release-notes.json");
    let notes: serde_json::Value = serde_json::from_str(include_str!("release-notes.json")).expect("valid release notes");
    assert_eq!(notes["version"].as_str(), Some(env!("CARGO_PKG_VERSION")), "Update release-notes.json for every release");
    assert!(notes["items"].as_array().is_some_and(|items| !items.is_empty() && items.iter().all(|v| v.as_str().is_some_and(|s| !s.trim().is_empty()))), "Release notes must describe the changes");
    tauri_build::build()
}
