#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use tauri::Manager;

struct Server(Mutex<Option<Child>>);

/// Locate server.py. In a bundle it is a packaged resource; in `tauri dev`
/// it sits next to the project. Resolving from the executable rather than
/// the working directory is what makes the bundled app work at all.
fn server_script(app: &tauri::AppHandle) -> Option<PathBuf> {
    if let Ok(p) = app
        .path()
        .resolve("server.py", tauri::path::BaseDirectory::Resource)
    {
        if p.exists() {
            return Some(p);
        }
    }
    let dev = std::env::current_dir().ok()?.join("server.py");
    if dev.exists() {
        return Some(dev);
    }
    let up = std::env::current_dir().ok()?.parent()?.join("server.py");
    up.exists().then_some(up)
}

fn python_bin() -> &'static str {
    if cfg!(windows) {
        "python"
    } else {
        "python3"
    }
}

fn stop_server(state: &Server) {
    if let Some(mut child) = state.0.lock().unwrap().take() {
        let _ = child.kill();
        let _ = child.wait();
    }
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
                eprintln!(
                    "DropIT: could not start the signalling server. \
                     Is Python installed and on PATH?"
                );
            }
            *app.state::<Server>().0.lock().unwrap() = child;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error building DropIT")
        // RunEvent::Exit covers quitting from the dock or tray, which
        // WindowEvent::Destroyed alone would miss — otherwise the Python
        // process is orphaned and keeps port 5000 bound.
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                stop_server(&app.state::<Server>());
            }
        });
}
