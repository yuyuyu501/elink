import tkinter as tk
from tkinter import messagebox
import pyglet
from server import Server
from client import ClientWindow
from discovery import Discovery, get_lan_ip


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title('Elink 远程控制')
        self.root.geometry('500x440')
        self.root.resizable(False, False)
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

        self.server_obj = None
        self.client_obj = None
        self.discovery = Discovery()
        self.server_running = False
        self.listen_port = 5901
        self._peers_data = []

        self._build_ui()
        self._update_ip_display()

    def _get_lan_ip(self):
        return get_lan_ip()

    def _update_ip_display(self):
        ip = self._get_lan_ip()
        self.ip_label.config(text=f'\u25cf 本机地址: {ip}')
        self.root.after(5000, self._update_ip_display)

    def _build_ui(self):
        self.root.configure(bg='#f0f0f0')

        header = tk.Label(self.root, text='Elink 远程控制', font=('', 16, 'bold'),
                          bg='#f0f0f0', pady=8)
        header.pack(fill=tk.X)

        sep = tk.Frame(self.root, height=1, bg='#cccccc')
        sep.pack(fill=tk.X, padx=10)

        top_frame = tk.Frame(self.root, padx=15, pady=12, bg='#f0f0f0')
        top_frame.pack(fill=tk.X)

        self.ip_label = tk.Label(top_frame, text='\u25cf 本机地址: 获取中...',
                                 font=('', 11, 'bold'), bg='#f0f0f0', fg='#333333')
        self.ip_label.pack(anchor=tk.W)

        ctrl_frame = tk.Frame(top_frame, bg='#f0f0f0')
        ctrl_frame.pack(fill=tk.X, pady=(8, 0))

        tk.Label(ctrl_frame, text='端口:', bg='#f0f0f0').pack(side=tk.LEFT)
        self.port_entry = tk.Entry(ctrl_frame, width=8)
        self.port_entry.insert(0, '5901')
        self.port_entry.pack(side=tk.LEFT, padx=(5, 0))

        btn_frame = tk.Frame(ctrl_frame, bg='#f0f0f0')
        btn_frame.pack(side=tk.RIGHT)

        self.start_btn = tk.Button(btn_frame, text='\u25b6 启动服务', width=10,
                                   command=self._start_service)
        self.start_btn.pack(side=tk.LEFT, padx=2)
        self.stop_btn = tk.Button(btn_frame, text='\u25a0 停止服务', width=10,
                                  command=self._stop_service, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=2)

        sep2 = tk.Frame(self.root, height=1, bg='#cccccc')
        sep2.pack(fill=tk.X, padx=10, pady=(0, 5))

        list_frame = tk.Frame(self.root, padx=15, pady=5, bg='#f0f0f0')
        list_frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(list_frame, text='局域网设备 (双击连接):', anchor=tk.W,
                 bg='#f0f0f0', font=('', 10)).pack(fill=tk.X)

        listbox_container = tk.Frame(list_frame, bg='#f0f0f0')
        listbox_container.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

        self.peer_listbox = tk.Listbox(listbox_container,
                                       font=('Consolas', 10),
                                       relief=tk.SUNKEN, bd=1,
                                       activestyle='none',
                                       selectbackground='#0078d7',
                                       selectforeground='white')
        self.peer_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = tk.Scrollbar(listbox_container, orient=tk.VERTICAL,
                                 command=self.peer_listbox.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.peer_listbox.config(yscrollcommand=scrollbar.set)

        self.peer_listbox.bind('<Double-Button-1>', self._on_peer_double_click)

        self.status_label = tk.Label(list_frame, text='状态: 服务已停止',
                                     fg='#888888', anchor=tk.W,
                                     bg='#f0f0f0')
        self.status_label.pack(fill=tk.X, pady=(5, 0))

        sep3 = tk.Frame(self.root, height=1, bg='#cccccc')
        sep3.pack(fill=tk.X, padx=10, pady=(5, 0))

        bottom_frame = tk.Frame(self.root, padx=15, pady=10, bg='#f0f0f0')
        bottom_frame.pack(fill=tk.X)

        tk.Button(bottom_frame, text='退出', width=10,
                  command=self._on_close).pack(side=tk.RIGHT)

    def _start_service(self):
        try:
            port = int(self.port_entry.get().strip())
            if port < 1 or port > 65535:
                raise ValueError
        except ValueError:
            messagebox.showerror('错误', '端口必须为 1-65535 的整数')
            return

        self.listen_port = port
        self.server_obj = Server(port=port)

        self.server_obj.start(
            on_status=lambda msg: self.root.after(
                0, lambda: self.status_label.config(text=f'状态: {msg}')),
            on_error=lambda msg: self.root.after(
                0, lambda: self.status_label.config(text=f'错误: {msg}', fg='red')),
            on_connect=lambda ip: self.root.after(
                0, lambda: self._on_remote_connect(ip)),
            on_disconnect=lambda: self.root.after(
                0, self._on_remote_disconnect),
        )

        self.discovery.start(
            port=port,
            on_add=lambda ip, port, name: self.root.after(
                0, lambda: self._on_peer_added(ip, port, name)),
            on_remove=lambda ip, port, name: self.root.after(
                0, lambda: self._on_peer_removed(ip, port, name)),
        )

        self.server_running = True
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.port_entry.config(state=tk.DISABLED)
        ip = self._get_lan_ip()
        self.status_label.config(text=f'状态: 服务运行中 ({ip}:{port})', fg='#2d7d2d')

    def _stop_service(self):
        self.discovery.stop()
        if self.server_obj:
            self.server_obj.stop()
            self.server_obj = None

        self.server_running = False
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.port_entry.config(state=tk.NORMAL)
        self.status_label.config(text='状态: 服务已停止', fg='#888888')

        self._peers_data.clear()
        self.peer_listbox.delete(0, tk.END)

    def _on_peer_added(self, ip, port, name):
        for pip, pport, _, _ in self._peers_data:
            if pip == ip and pport == port:
                return
        display = f'    {ip:15}  {name}'
        self._peers_data.append((ip, port, name, display))
        self.peer_listbox.insert(tk.END, display)
        self._update_peer_count()

    def _on_peer_removed(self, ip, port, name):
        for i, (pip, pport, _, _) in enumerate(self._peers_data):
            if pip == ip and pport == port:
                self._peers_data.pop(i)
                self.peer_listbox.delete(i)
                break
        self._update_peer_count()

    def _update_peer_count(self):
        count = len(self._peers_data)
        if self.server_running:
            text = f'状态: 服务运行中 \u00b7 已发现 {count} 台设备' if count else '状态: 服务运行中'
            self.status_label.config(text=text, fg='#2d7d2d')

    def _on_peer_double_click(self, event):
        selection = self.peer_listbox.curselection()
        if not selection:
            return
        idx = selection[0]
        if idx >= len(self._peers_data):
            return
        ip, port, name, _ = self._peers_data[idx]
        self._start_client(ip, port)

    def _start_client(self, host, port):
        self.root.withdraw()
        self.root.update()

        self.client_obj = ClientWindow(
            host, port,
            on_disconnect=lambda: pyglet.app.exit(),
        )
        pyglet.app.run()
        self.client_obj = None
        self.root.deiconify()

    def _on_remote_connect(self, ip):
        self.status_label.config(text=f'状态: 被 {ip} 控制中', fg='#cc7000')

    def _on_remote_disconnect(self):
        self.status_label.config(text='状态: 服务运行中', fg='#2d7d2d')

    def _on_close(self):
        self._stop_service()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self):
        self.root.mainloop()


if __name__ == '__main__':
    App().run()
