import customtkinter as ctk

from .auth import AuthError
from .background import run_in_background


class LoginFrame(ctk.CTkFrame):
    def __init__(self, master, auth, gateway_url, on_login, message=None, config_error=None):
        super().__init__(master, fg_color="transparent")

        self.auth = auth
        self.on_login = on_login
        self.busy = False

        container = ctk.CTkFrame(
            self,
            width=420,
            corner_radius=15
        )
        container.place(relx=0.5, rely=0.5, anchor="center")

        title = ctk.CTkLabel(
            container,
            text="Welcome Back",
            font=("Arial", 30, "bold")
        )
        title.pack(padx=45, pady=(35, 10))

        subtitle = ctk.CTkLabel(
            container,
            text="Sign in to Eddie Transfer Service",
            text_color="gray"
        )
        subtitle.pack(pady=(0, 30))

        self.username_entry = ctk.CTkEntry(
            container,
            width=320,
            height=42,
            placeholder_text="Username"
        )
        self.username_entry.pack(pady=10)

        self.password_entry = ctk.CTkEntry(
            container,
            width=320,
            height=42,
            placeholder_text="Password",
            show="*"
        )
        self.password_entry.pack(pady=10)

        self.error_label = ctk.CTkLabel(
            container,
            text=config_error or message or "",
            text_color="#ff6b6b",
            wraplength=320
        )
        self.error_label.pack(pady=5)

        self.login_button = ctk.CTkButton(
            container,
            text="Sign In",
            width=320,
            height=42,
            command=self.login,
            state="disabled" if config_error else "normal"
        )
        self.login_button.pack(pady=(10, 15))

        note = ctk.CTkLabel(
            container,
            text=f"Gateway: {gateway_url}" if gateway_url else "",
            font=("Arial", 11),
            text_color="gray"
        )
        note.pack(pady=(0, 30))

        self.password_entry.bind(
            "<Return>",
            lambda event: self.login()
        )
        self.username_entry.focus_set()

    def login(self):
        if self.busy or self.auth is None:
            return

        username = self.username_entry.get().strip()
        password = self.password_entry.get()

        if not username or not password:
            self.error_label.configure(
                text="Please enter username and password."
            )
            return

        # The password leaves the entry field straight away and is only
        # held by the sign-in request.
        self.password_entry.delete(0, "end")
        self.busy = True
        self.login_button.configure(state="disabled", text="Signing in...")
        self.error_label.configure(text="")

        run_in_background(
            self,
            lambda: self.auth.login(username, password),
            self._login_done
        )

    def _login_done(self, session, error):
        self.busy = False
        self.login_button.configure(state="normal", text="Sign In")

        if error is not None:
            message = error.message if isinstance(error, AuthError) else "Sign-in failed unexpectedly."
            self.error_label.configure(text=message)
            self.password_entry.focus_set()
            return

        self.on_login(session)
