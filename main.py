import tkinter as tk
from tkinter import messagebox
import socket
import threading
from server import Server
from client import ClientController


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title('Elink 远程控制')
        self.root.geometry('450x360')
        self.root.resizable(False, False)

        self.mode = tk.StringVar(value='client')
        self.server_obj = None
        self.client_obj = None
        self.server_thread = None

        self._build_ui()

    def _build_ui(self):
        f = tk.Frame(self.root, padx=30, pady=20)
        f.pack(fill=tk.BOTH, expand=True)

        tk.Label(f, text='Elink 远程控制', font=('', 18, 'bold')).pack(pady=(0, 20))

        mode_frame = tk.LabelFrame(f, text='模式选择', padx=10, pady=10)
        mode_frame.pack(fill=tk.X, pady=5)

        tk.Radiobutton(mode_frame, text='控制别人', variable=self.mode,
                       value='client', command=self._on_mode_change).pack(anchor=tk.W)
        tk.Radiobutton(mode_frame, text='被别人控制', variable=self.mode,
                       value='server', command=self._on_mode_change).pack(anchor=tk.W)

        self.port_frame = tk.Frame(f)
        self.port_frame.pack(fill=tk.X, pady=8)

        tk.Label(self.port_frame, text='本机端口:').pack(side=tk.LEFT)
        self.port_entry = tk.Entry(self.port_frame, width=10)
        self.port_entry.insert(0, '5901')
        self.port_entry.pack(side=tk.LEFT, padx=(5, 0))

        self.addr_frame = tk.Frame(f)
        self.addr_frame.pack(fill=tk.X, pady=5)

        tk.Label(self.addr_frame, text='目标地址:').pack(side=tk.LEFT)
        self.addr_entry = tk.Entry(self.addr_frame, width=18)
        self.addr_entry.pack(side=tk.LEFT, padx=(5, 0))
        self.addr_entry.insert(0, '192.168.1.100')

        tk.Label(self.addr_frame, text='端口:').pack(side=tk.LEFT, padx=(10, 0))
        self.target_port_entry = tk.Entry(self.addr_frame, width=8)
        self.target_port_entry.insert(0, '5901')
        self.target_port_entry.pack(side=tk.LEFT, padx=(5, 0))

        self.status_label = tk.Label(f, text='', fg='gray')
        self.status_label.pack(pady=5)

        btn_frame = tk.Frame(f)
        btn_frame.pack(pady=(5, 0))

        tk.Button(btn_frame, text='启动', width=10, command=self._start).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text='退出', width=10, command=self.root.quit).pack(side=tk.LEFT, padx=5)

        self._on_mode_change()

    def _on_mode_change(self):
        if self.mode.get() == 'client':
            self.addr_frame.pack(fill=tk.X, pady=5)
        else:
            self.addr_frame.pack_forget()

    def _start(self):
        try:
            port = int(self.port_entry.get().strip())
            if port < 1 or port > 65535:
                raise ValueError
        except ValueError:
            messagebox.showerror('错误', '端口必须为 1-65535 的整数')
            return

        if self.mode.get() == 'client':
            addr = self.addr_entry.get().strip()
            if not addr:
                messagebox.showerror('错误', '请输入目标地址')
                return
            try:
                tport = int(self.target_port_entry.get().strip())
            except ValueError:
                messagebox.showerror('错误', '目标端口必须为 1-65535 的整数')
                return
            self._start_client(addr, tport, port)
        else:
            self._start_server(port)

    def _start_client(self, host, port, local_port):
        self.root.withdraw()

        win = tk.Toplevel(self.root)
        win.title(f'Elink - 控制 {host}:{port}')
        win.geometry('1024x700')
        win.protocol('WM_DELETE_WINDOW', lambda: self._stop_client(win))

        self.client_obj = ClientController(win, host, port, on_disconnect=lambda: self._stop_client(win))

    def _stop_client(self, win):
        if self.client_obj:
            self.client_obj.stop()
            self.client_obj = None
        try:
            win.destroy()
        except tk.TclError:
            pass
        self.root.deiconify()

    def _start_server(self, port):
        self.root.withdraw()

        win = tk.Toplevel(self.root)
        win.title('Elink - 被别人控制')
        win.geometry('350x200')
        win.resizable(False, False)
        win.protocol('WM_DELETE_WINDOW', lambda: self._stop_server(win))

        f = tk.Frame(win, padx=20, pady=20)
        f.pack(fill=tk.BOTH, expand=True)

        tk.Label(f, text='被控端运行中', font=('', 14, 'bold')).pack(pady=(0, 10))

        try:
            hostname = socket.gethostbyname(socket.gethostname())
        except Exception:
            hostname = '无法获取'

        tk.Label(f, text=f'本机地址: {hostname}').pack()
        tk.Label(f, text=f'监听端口: {port}').pack(pady=2)

        self.srv_status = tk.Label(f, text='等待连接...', fg='blue')
        self.srv_status.pack(pady=5)

        tk.Button(f, text='停止', width=10, command=lambda: self._stop_server(win)).pack(pady=10)

        self.server_obj = Server(port=port)

        self.server_obj.start(
            on_status=lambda msg: self.root.after(0, lambda: self.srv_status.config(text=msg)),
            on_error=lambda msg: self.root.after(0, lambda: self.srv_status.config(text=f'错误: {msg}')),
        )

    def _stop_server(self, win):
        if self.server_obj:
            self.server_obj.stop()
            self.server_obj = None
        try:
            win.destroy()
        except tk.TclError:
            pass
        self.root.deiconify()

    def run(self):
        self.root.mainloop()


if __name__ == '__main__':
    App().run()
