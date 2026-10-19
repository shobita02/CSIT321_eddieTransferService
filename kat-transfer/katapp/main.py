from pathlib import Path

import customtkinter as ctk

from . import tokenstore
from .appconfig import default_config_path, gateway_settings
from .auth import AuthError, GatewayAuth
from .background import run_in_background
from .dashboard import DashboardFrame
from .login import LoginFrame


class EddieTransferApp(ctk.CTk):
    def __init__(self, config_path=None):
        super().__init__()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("Eddie Transfer Service")
        self.geometry("900x680")
        self.minsize(750, 580)

        self.config_path = Path(config_path) if config_path else default_config_path()
        self.current_frame = None
        self.session = None
        self.auth = None
        self.gateway_url = ""
        self.config_error = None

        try:
            gateway = gateway_settings(self.config_path)
            self.gateway_url = gateway.url
            self.auth = GatewayAuth(gateway)
        except FileNotFoundError:
            self.config_error = f"Configuration file not found: {self.config_path}"
        except (ValueError, OSError) as exc:
            self.config_error = f"Configuration problem: {exc}"

        # Closing the window is the same as Quit: finish the batch first.
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.resume_saved_session()

    def clear_frame(self):
        if self.current_frame is not None:
            self.current_frame.destroy()
            self.current_frame = None

    def show_message(self, text):
        self.clear_frame()
        self.current_frame = ctk.CTkLabel(self, text=text, font=ctk.CTkFont(size=18), text_color="gray")
        self.current_frame.pack(fill="both", expand=True)

    # ==========================
    # Start-up
    # ==========================

    def resume_saved_session(self):
        """Skip sign-in when a saved token has not expired and the gateway still accepts it."""
        saved = tokenstore.load(self.gateway_url) if self.auth else None
        if saved is None:
            self.show_login()
            return

        self.show_message("Checking saved sign-in...")

        def done(session, error):
            if error is None:
                self.show_dashboard(session)
            elif isinstance(error, AuthError) and error.kind in ("credentials", "read_only"):
                tokenstore.clear(self.gateway_url)
                self.show_login(error.message)
            else:
                # Gateway unreachable: keep the token; the service retries until it is back.
                self.show_dashboard(saved)

        run_in_background(self, lambda: self.auth.me(saved.token), done)

    # ==========================
    # Screens
    # ==========================

    def show_login(self, message=None):
        self.clear_frame()
        self.current_frame = LoginFrame(
            self,
            auth=self.auth,
            gateway_url=self.gateway_url,
            on_login=self.signed_in,
            message=message,
            config_error=self.config_error
        )
        self.current_frame.pack(fill="both", expand=True)

    def signed_in(self, session):
        if not tokenstore.save(self.gateway_url, session):
            session_note = "This computer has no credential store; you will sign in again next time."
        else:
            session_note = None
        self.show_dashboard(session)
        if session_note:
            self.current_frame.add_log(session_note)

    def show_dashboard(self, session):
        self.clear_frame()
        self.session = session
        self.current_frame = DashboardFrame(
            self,
            session=session,
            config_path=self.config_path,
            on_sign_out=self.signed_out,
            on_auth_failed=self.token_refused,
            on_quit=self.destroy
        )
        self.current_frame.pack(fill="both", expand=True)

    # ==========================
    # Leaving the dashboard
    # ==========================

    def signed_out(self):
        token = self.session.token
        tokenstore.clear(self.gateway_url)
        self.session = None
        self.show_login("You have signed out.")
        # Revoke on the server too, without holding up the window.
        run_in_background(self, lambda: self.auth.logout(token), lambda *_: None)

    def token_refused(self, message):
        tokenstore.clear(self.gateway_url)
        self.session = None
        self.show_login(message)

    def on_close(self):
        if isinstance(self.current_frame, DashboardFrame):
            self.current_frame.quit()
        else:
            self.destroy()


def run(config_path=None):
    app = EddieTransferApp(config_path)
    app.mainloop()


if __name__ == "__main__":
    run()
