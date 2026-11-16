#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use tauri::Manager;

struct Server(Mutex<Option<Child>>);

fn server_script(app: &tauri::AppHandle) -> Option<PathBuf> {
    if let Ok(p) = app.path().resolve("server.py", tauri::path::BaseDirectory::Resource) {
        if p.exists() {
            return Some(p);
        }
    }
    let dev = std::env::current_dir().ok()?.join("server.py");
    dev.exists().then_some(dev)
}

fn python_bin() -> &'static str {
    if cfg!(windows) { "python" } else { "python3" }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_clipboard_manager::init())
        .manage(Server(Mutex::new(None)))
        .setup(|app| {
            let handle = app.handle().clone();
            let child = server_script(&handle).and_then(|script| {
                let cwd = script.parent()?.to_path_buf();
                Command::new(python_bin())
                    .arg(&script)
                    .current_dir(cwd)
                    .env("PORT", "5000")
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .spawn()
                    .ok()
            });
            if child.is_none() {
                eprintln!("DropIT: could not start the bundled signalling server");
            }
            *app.state::<Server>().0.lock().unwrap() = child;
            Ok(())
        })
        // Destroyed fires per window; Exit covers quitting from the dock/tray too.
        .on_window_event(|window, event| {
            if matches!(event, tauri::WindowEvent::Destroyed) {
                if let Some(mut c) = window.state::<Server>().0.lock().unwrap().take() {
                    let _ = c.kill();
                    let _ = c.wait();
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error building DropIT")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                if let Some(mut c) = app.state::<Server>().0.lock().unwrap().take() {
                    let _ = c.kill();
                    let _ = c.wait();
                }
            }
        });
}
