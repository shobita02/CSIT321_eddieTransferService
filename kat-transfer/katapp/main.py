import customtkinter as ctk

from .login import LoginFrame
from .dashboard import DashboardFrame


class EddieTransferApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("Eddie Transfer Service")
        self.geometry("900x650")
        self.minsize(750, 550)

        self.current_frame = None

        self.show_login()

    def clear_frame(self):
        if self.current_frame is not None:
            self.current_frame.destroy()
            self.current_frame = None

    def show_login(self):
        self.clear_frame()

        self.current_frame = LoginFrame(
            self,
            on_login=self.show_dashboard
        )
        self.current_frame.pack(
            fill="both",
            expand=True
        )

    def show_dashboard(self, username):
        self.clear_frame()

        self.current_frame = DashboardFrame(
            self,
            username=username,
            on_logout=self.show_login
        )
        self.current_frame.pack(
            fill="both",
            expand=True
        )


if __name__ == "__main__":
    app = EddieTransferApp()
    app.mainloop()
