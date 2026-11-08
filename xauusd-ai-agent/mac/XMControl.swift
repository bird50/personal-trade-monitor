import AppKit
import WebKit
import UserNotifications

final class Controller: NSObject, NSApplicationDelegate, WKScriptMessageHandlerWithReply, WKNavigationDelegate, UNUserNotificationCenterDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var menuItem: NSStatusItem!
    var timer: Timer?
    var root = ""
    var token = ""
    var polling = false
    var failures = 0
    var offlineIncident = false
    var manuallyStopped = false
    let notices = UNUserNotificationCenter.current()
    let defaults = UserDefaults.standard
    var trustedPage: URL { Bundle.main.resourceURL!.appendingPathComponent("dashboard/index.html").standardizedFileURL }
    var cursor: Int { get { defaults.integer(forKey: "eventCursor") } set { defaults.set(newValue, forKey: "eventCursor") } }

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard let resource = Bundle.main.resourceURL,
              let data = try? Data(contentsOf: resource.appendingPathComponent("project.json")),
              let config = try? JSONSerialization.jsonObject(with: data) as? [String: String],
              let path = config["root"] else { NSApp.terminate(nil); return }
        root = path
        token = readToken()
        notices.delegate = self
        let content = WKUserContentController()
        content.addScriptMessageHandler(self, contentWorld: .page, name: "control")
        let conf = WKWebViewConfiguration()
        conf.userContentController = content
        conf.preferences.javaScriptCanOpenWindowsAutomatically = false
        web = WKWebView(frame: .zero, configuration: conf)
        web.navigationDelegate = self
        web.setValue(false, forKey: "drawsBackground")
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 860), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "XM Control · Trade Alerts"
        window.minSize = NSSize(width: 1080, height: 740)
        window.appearance = NSAppearance(named: .darkAqua)
        window.contentView = web
        window.isReleasedWhenClosed = false
        window.center()
        web.loadFileURL(resource.appendingPathComponent("dashboard/index.html"), allowingReadAccessTo: resource.appendingPathComponent("dashboard"))
        let main = NSMenu()
        let appMenu = NSMenuItem()
        let submenu = NSMenu()
        submenu.addItem(withTitle: "แสดง XM Control", action: #selector(showWindow), keyEquivalent: "0").target = self
        submenu.addItem(NSMenuItem.separator())
        submenu.addItem(withTitle: "ออกจาก XM Control", action: #selector(quit), keyEquivalent: "q").target = self
        appMenu.submenu = submenu
        main.addItem(appMenu)
        NSApp.mainMenu = main
        menuItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        menuItem.button?.title = "◈ XM"
        menuItem.menu = submenu.copy() as? NSMenu
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in self?.poll() }
        poll()
    }

    func readToken() -> String {
        guard let text = try? String(contentsOfFile: root + "/.env", encoding: .utf8) else { return "" }
        for raw in text.components(separatedBy: .newlines) {
            let line = raw.trimmingCharacters(in: .whitespaces)
            let pieces = line.components(separatedBy: "=")
            if pieces.first == "API_TOKEN" {
                return pieces.dropFirst().joined(separator: "=").trimmingCharacters(in: .whitespaces).trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
            }
        }
        return ""
    }

    @objc func showWindow() { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
    @objc func quit() { NSApp.terminate(nil) }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showWindow(); return true }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        let allowed = navigationAction.request.url?.isFileURL == true && navigationAction.request.url?.standardizedFileURL == trustedPage
        decisionHandler(allowed ? .allow : .cancel)
    }

    func request(_ path: String, paused: Bool? = nil, completion: @escaping ([String: Any]?) -> Void) {
        guard !token.isEmpty, let url = URL(string: "http://127.0.0.1:8002" + path) else { completion(nil); return }
        var req = URLRequest(url: url, timeoutInterval: 4)
        req.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        if let paused = paused {
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: ["paused": paused])
        }
        URLSession.shared.dataTask(with: req) { data, response, _ in
            var result: [String: Any]?
            if (response as? HTTPURLResponse)?.statusCode == 200, let data = data { result = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] }
            DispatchQueue.main.async { completion(result) }
        }.resume()
    }

    func notificationState(_ completion: @escaping (String) -> Void) {
        notices.getNotificationSettings { settings in
            let value: String
            switch settings.authorizationStatus { case .authorized, .provisional: value = "enabled"; case .denied: value = "denied"; default: value = "not_requested" }
            DispatchQueue.main.async { completion(value) }
        }
    }

    func sendNotice(id: String, title: String, message: String, completion: ((Bool) -> Void)? = nil) {
        notificationState { state in
            guard state == "enabled" else { completion?(false); return }
            let content = UNMutableNotificationContent()
            content.title = "XM Control · " + title
            content.body = message
            content.sound = .default
            self.notices.add(UNNotificationRequest(identifier: id, content: content, trigger: nil)) { error in
                let accepted = error == nil
                self.notices.getDeliveredNotifications { delivered in
                    let report: [String: Any] = ["accepted": accepted, "delivered": delivered.contains { $0.request.identifier == id }, "delivered_count": delivered.count, "checked_at": Date().timeIntervalSince1970]
                    if let data = try? JSONSerialization.data(withJSONObject: report) {
                        let file = URL(fileURLWithPath: self.root + "/.runtime/mac-notifications.json")
                        try? data.write(to: file, options: .atomic)
                        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
                    }
                }
                DispatchQueue.main.async { completion?(accepted) }
            }
        }
    }

    func poll() {
        guard !polling else { return }
        polling = true
        request("/dashboard/state?after_event_id=\(cursor)") { state in
            self.polling = false
            guard let state = state else {
                self.failures += 1
                self.menuItem.button?.title = "◈ XM · !"
                if self.failures >= 3 && !self.offlineIncident && !self.manuallyStopped {
                    self.offlineIncident = true
                    self.sendNotice(id: UUID().uuidString, title: "บริการในเครื่องขาดการเชื่อมต่อ", message: "เปิด XM Control เพื่อตรวจสถานะและเริ่มบริการอีกครั้ง")
                }
                return
            }
            self.failures = 0
            if self.offlineIncident {
                self.offlineIncident = false
                self.sendNotice(id: UUID().uuidString, title: "บริการกลับมาทำงานแล้ว", message: "เชื่อมต่อระบบแจ้งเตือนสัญญาณได้อีกครั้ง")
            }
            // The service decides readiness: fresh MT5 feed, warmed-up strategy and running workers.
            let ready = state["mode"] as? String == "alert_only" && state["ready"] as? Bool == true
            self.menuItem.button?.title = ready ? "◈ XM · ●" : "◈ XM · !"
            let events = state["events"] as? [[String: Any]] ?? []
            if self.defaults.bool(forKey: "eventBaseline") {
                for event in events {
                    if let id = event["id"] as? Int, id > self.cursor {
                        self.sendNotice(id: "event-\(id)", title: event["title"] as? String ?? "เหตุการณ์ใหม่", message: event["message"] as? String ?? "")
                        self.cursor = id
                    }
                }
            } else {
                self.cursor = state["latest_event_id"] as? Int ?? 0
                self.defaults.set(true, forKey: "eventBaseline")
            }
        }
    }

    func runService(_ action: String, completion: @escaping ([String: Any]) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            let proc = Process(), pipe = Pipe()
            proc.executableURL = URL(fileURLWithPath: self.root + "/.venv/bin/python")
            proc.arguments = [self.root + "/scripts/control_system.py", action]
            proc.currentDirectoryURL = URL(fileURLWithPath: self.root)
            proc.standardOutput = pipe
            proc.standardError = FileHandle.nullDevice
            var result: [String: Any] = ["ok": false, "error": "เรียกตัวควบคุมบริการไม่สำเร็จ"]
            do {
                try proc.run()
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                proc.waitUntilExit()
                result = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? result
            } catch { }
            let output = result
            DispatchQueue.main.async {
                if output["ok"] as? Bool == true && action != "status" {
                    self.manuallyStopped = action == "stop"
                    self.failures = 0
                    self.offlineIncident = false
                }
                completion(output)
            }
        }
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage, replyHandler: @escaping (Any?, String?) -> Void) {
        guard message.frameInfo.isMainFrame, message.frameInfo.request.url?.standardizedFileURL == trustedPage,
              let body = message.body as? [String: Any], let action = body["action"] as? String else { replyHandler(nil, "Invalid request"); return }
        switch action {
        case "state":
            request("/dashboard/state") { result in
                self.notificationState { permission in
                    var data = result ?? ["connected": false]
                    data["notifications"] = permission
                    replyHandler(data, nil)
                }
            }
        case "pause":
            guard let paused = body["paused"] as? Bool else { replyHandler(nil, "Invalid request"); return }
            request("/dashboard/pause", paused: paused) { result in replyHandler(result ?? ["error": "บันทึกการปิดเสียงไม่สำเร็จ กรุณาตรวจบริการ"], nil) }
        case "start", "stop", "status": runService(action) { replyHandler($0, nil) }
        case "notifications":
            notices.requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in
                self.notificationState { replyHandler(["notifications": $0], nil) }
            }
        case "testNotification":
            notificationState { permission in
                guard permission == "enabled" else { replyHandler(["notifications": permission, "sent": false], nil); return }
                self.sendNotice(id: UUID().uuidString, title: "ทดสอบการแจ้งเตือน", message: "พร้อมแจ้งจังหวะเข้า trade ขณะ XM Control เปิดอยู่") { sent in
                    replyHandler(["notifications": permission, "sent": sent], nil)
                }
            }
        default: replyHandler(nil, "Unknown action")
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification, withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) { completionHandler([.banner, .sound, .list]) }
    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse, withCompletionHandler completionHandler: @escaping () -> Void) { DispatchQueue.main.async { self.showWindow(); completionHandler() } }
}

let app = NSApplication.shared
let controller = Controller()
app.setActivationPolicy(.regular)
app.delegate = controller
app.run()
