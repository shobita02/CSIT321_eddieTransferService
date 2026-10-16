import customtkinter as ctk


class LoginFrame(ctk.CTkFrame):
    def __init__(self, master, on_login):
        super().__init__(master, fg_color="transparent")

        self.on_login = on_login

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
            text="",
            text_color="#ff6b6b"
        )
        self.error_label.pack(pady=5)

        login_button = ctk.CTkButton(
            container,
            text="Sign In",
            width=320,
            height=42,
            command=self.login
        )
        login_button.pack(pady=(10, 15))

        note = ctk.CTkLabel(
            container,
            text="Development mode - Authentication not connected",
            font=("Arial", 11),
            text_color="gray"
        )
        note.pack(pady=(0, 30))

        self.password_entry.bind(
            "<Return>",
            lambda event: self.login()
        )

    def login(self):
        username = self.username_entry.get().strip()
        password = self.password_entry.get()

        if not username or not password:
            self.error_label.configure(
                text="Please enter username and password."
            )
            return

        # UI prototype only.
        # This does NOT authenticate the user.
        # Real authentication will be integrated later.
        self.password_entry.delete(0, "end")
        self.on_login(username)
