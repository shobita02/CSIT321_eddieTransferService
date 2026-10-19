
"""Status screen for Eddie Transfer Service. Transfers run until Quit or Sign out."""

from __future__ import annotations

from datetime import datetime

import customtkinter as ctk

from .controller import TransferController

OK = "#4CAF50"
WARN = "#E5A54B"
BAD = "#E57373"


class DashboardFrame(ctk.CTkFrame):

    def __init__(self, master, session, config_path, on_sign_out, on_auth_failed, on_quit):
        super().__init__(master, fg_color="#242424")

        self.session = session
        self.config_path = config_path
        self.on_sign_out = on_sign_out
        self.on_auth_failed = on_auth_failed
        self.on_quit = on_quit

        self.controller = None
        self.poll_job = None
        self.closing_action = None  # "quit" or "sign_out" while finishing a batch
        self.last_cycles = 0
        self.last_failures = 0
        self.last_sent = 0
        self.last_warning = ""
        self.last_transfer = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        # ==========================
        # Header
        # ==========================

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=30, pady=(20, 10))
        header.grid_columnconfigure(0, weight=1)

        title = ctk.CTkLabel(
            header,
            text="Transfer Dashboard",
            font=ctk.CTkFont(size=32, weight="bold")
        )
        title.grid(row=0, column=0, sticky="w")

        self.sign_out_button = ctk.CTkButton(
            header,
            text="Sign out",
            width=120,
            height=42,
            fg_color="#555555",
            hover_color="#666666",
            command=self.sign_out
        )
        self.sign_out_button.grid(row=0, column=1, sticky="e", padx=(0, 10))

        self.quit_button = ctk.CTkButton(
            header,
            text="Quit",
            width=120,
            height=42,
            fg_color="#B83232",
            hover_color="#922626",
            command=self.quit
        )
        self.quit_button.grid(row=0, column=2, sticky="e")

        expires = (
            session.expires_at.astimezone().strftime("%d %b %Y %H:%M")
            if session.expires_at else "never"
        )
        self.account_label = ctk.CTkLabel(
            self,
            text=f"Signed in as {session.username}  ·  token expires {expires}",
            font=ctk.CTkFont(size=16),
            text_color=WARN if session.expires_soon() else "gray"
        )
        self.account_label.grid(row=1, column=0, sticky="w", padx=30, pady=(0, 4))

        self.expiry_warning = ctk.CTkLabel(
            self,
            text=(
                "Your token expires soon. Sign out and sign in again to renew it."
                if session.expires_soon() else ""
            ),
            text_color=WARN
        )
        self.expiry_warning.grid(row=2, column=0, sticky="w", padx=30)

        # ==========================
        # Service Status
        # ==========================

        status_frame = ctk.CTkFrame(self, corner_radius=10)
        status_frame.grid(row=3, column=0, sticky="ew", padx=30, pady=10)
        status_frame.grid_columnconfigure(1, weight=1)

        def row(label, index):
            ctk.CTkLabel(
                status_frame,
                text=label,
                font=ctk.CTkFont(size=16, weight="bold")
            ).grid(row=index, column=0, sticky="nw", padx=(20, 15), pady=6)
            value = ctk.CTkLabel(
                status_frame,
                text="--",
                font=ctk.CTkFont(size=16),
                justify="left",
                anchor="w",
                wraplength=520
            )
            value.grid(row=index, column=1, sticky="w", pady=6)
            return value

        self.status_label = row("Service", 0)
        self.gateway_label = row("Gateway", 1)
        self.db_label = row("KAT database", 2)
        self.rows_label = row("Rows sent", 3)
        self.last_label = row("Last transfer", 4)

        self.restart_button = ctk.CTkButton(
            status_frame,
            text="Restart",
            width=160,
            height=40,
            fg_color="#16864F",
            hover_color="#11683D",
            command=self.start_transfer
        )

        # ==========================
        # Activity Log
        # ==========================

        log_frame = ctk.CTkFrame(self, corner_radius=10)
        log_frame.grid(row=4, column=0, sticky="nsew", padx=30, pady=(10, 25))

        ctk.CTkLabel(
            log_frame,
            text="Activity Log",
            font=ctk.CTkFont(size=22, weight="bold")
        ).pack(anchor="w", padx=20, pady=(20, 10))

        self.log_box = ctk.CTkTextbox(
            log_frame,
            height=160,
            font=ctk.CTkFont(size=14)
        )
        self.log_box.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        self.log_box.configure(state="disabled")

        self.start_transfer()
        self.poll()

    # ==========================
    # Activity Log
    # ==========================

    def add_log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{timestamp}] {message}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    # ==========================
    # Start Transfer
    # ==========================

    def start_transfer(self):
        self.restart_button.grid_forget()
        self.last_cycles = self.last_failures = self.last_sent = 0
        self.controller = TransferController(self.config_path, self.session)
        try:
            self.controller.start()
        except Exception as exc:  # noqa: BLE001 - e.g. missing config or database
            self.show_crash(f"{type(exc).__name__}: {exc}")
            return
        self.status_label.configure(text="Running", text_color=OK)
        # The service contacts the gateway only when KAT has rows to send.
        self.gateway_label.configure(text="Waiting for new KAT data", text_color="gray")
        self.db_label.configure(text=self.controller.snapshot()["db_path"])
        self.add_log("Transfer service started. Rows are sent as KAT records them.")

    def show_crash(self, message):
        self.status_label.configure(text=f"Stopped by an error: {message}", text_color=BAD)
        self.add_log(f"ERROR: {message}")
        self.restart_button.grid(row=5, column=1, sticky="w", pady=(6, 16))

    # ==========================
    # Status Polling
    # ==========================

    def poll(self):
        controller = self.controller

        if controller is not None:
            for kind, message in controller.get_events():
                if kind == "warning":
                    self.last_warning = message
                    self.add_log(message)
                elif kind == "auth_failed":
                    self.poll_job = None
                    self.on_auth_failed(message)
                    return
                elif kind == "crashed":
                    if self.closing_action is None:
                        self.show_crash(message)
                elif kind == "stopped":
                    self.add_log("Transfer service stopped.")

            self.update_status(controller.snapshot())

            if self.closing_action and not controller.is_active():
                action, self.closing_action = self.closing_action, None
                self.poll_job = None
                if action == "quit":
                    self.on_quit()
                else:
                    self.on_sign_out()
                return

        self.poll_job = self.after(1000, self.poll)

    def update_status(self, snap):
        sent = snap["experimental_sent"] + snap["environment_sent"] + snap["experiments_sent"]
        if sent > self.last_sent:
            self.last_transfer = datetime.now()
            self.last_sent = sent

        if self.closing_action is None and self.controller.is_active():
            if snap["failures"] > self.last_failures:
                reason = self.last_warning or "the gateway did not respond"
                self.gateway_label.configure(text=f"Retrying - {reason}", text_color=WARN)
            elif snap["cycles"] > self.last_cycles:
                self.gateway_label.configure(text="Connected", text_color=OK)
        self.last_failures = snap["failures"]
        self.last_cycles = snap["cycles"]

        self.rows_label.configure(
            text=(
                f"{snap['experimental_sent']} measurement, "
                f"{snap['environment_sent']} environment, "
                f"{snap['experiments_sent']} experiment rows this session"
            )
        )
        self.last_label.configure(
            text=self.last_transfer.strftime("%H:%M:%S") if self.last_transfer else "nothing sent yet"
        )

    # ==========================
    # Quit and Sign out
    # ==========================

    def finish(self, action):
        """Stop after the current batch, then run the action once the worker has ended."""
        if self.closing_action is not None:
            return
        self.closing_action = action
        self.sign_out_button.configure(state="disabled")
        self.quit_button.configure(state="disabled")
        self.restart_button.grid_forget()
        if self.controller is not None:
            self.controller.stop()
        self.status_label.configure(text="Finishing current batch...", text_color="gray")
        self.add_log("Finishing current batch...")
        if self.poll_job is not None:
            self.after_cancel(self.poll_job)
        self.poll_job = self.after(100, self.poll)

    def quit(self):
        self.finish("quit")

    def sign_out(self):
        self.finish("sign_out")

    def destroy(self):
        if self.poll_job is not None:
            self.after_cancel(self.poll_job)
            self.poll_job = None
        super().destroy()
