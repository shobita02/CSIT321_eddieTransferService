
"""Dashboard for Eddie Transfer Service."""

from __future__ import annotations

import sys

import threading
import time
from datetime import datetime
from pathlib import Path

import customtkinter as ctk

from .controller import TransferController


class DashboardFrame(ctk.CTkFrame):

    def __init__(self, master, username, on_logout):
        super().__init__(master, fg_color="#242424")

        self.username = username
        self.on_logout = on_logout

        if getattr(sys, "frozen", False):
            project_root = Path(sys.executable).resolve().parent
        else:
            project_root = Path(__file__).resolve().parent.parent

        config_path = project_root / "config.local.toml"

        # Real transfer service connected to the local test gateway.
        self.controller = TransferController(
            config_path=config_path,
            simulation=False
        )

        self.logout_pending = False
        self.poll_job = None

        # Gateway monitoring
        self.gateway_check_running = False
        self.gateway_check_interval_s = 5.0
        self.last_gateway_check = 0.0
        self.closing = False

        # Window layout
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(5, weight=1)

        # ==========================
        # Header
        # ==========================

        header = ctk.CTkFrame(
            self,
            fg_color="transparent"
        )
        header.grid(
            row=0,
            column=0,
            sticky="ew",
            padx=30,
            pady=(20, 10)
        )

        header.grid_columnconfigure(0, weight=1)

        title = ctk.CTkLabel(
            header,
            text="Transfer Dashboard",
            font=ctk.CTkFont(size=32, weight="bold")
        )
        title.grid(
            row=0,
            column=0,
            sticky="w"
        )

        self.logout_button = ctk.CTkButton(
            header,
            text="Logout",
            width=150,
            height=42,
            fg_color="#555555",
            hover_color="#666666",
            command=self.logout
        )
        self.logout_button.grid(
            row=0,
            column=1,
            sticky="e"
        )

        welcome = ctk.CTkLabel(
            self,
            text=f"Welcome, {self.username}",
            font=ctk.CTkFont(size=16),
            text_color="gray"
        )
        welcome.grid(
            row=1,
            column=0,
            sticky="w",
            padx=30,
            pady=(0, 20)
        )

        # ==========================
        # Service Status
        # ==========================

        status_frame = ctk.CTkFrame(
            self,
            corner_radius=10
        )
        status_frame.grid(
            row=2,
            column=0,
            sticky="ew",
            padx=30,
            pady=10
        )

        self.status_label = ctk.CTkLabel(
            status_frame,
            text="Service Status: Stopped",
            font=ctk.CTkFont(size=22)
        )
        self.status_label.pack(
            pady=(25, 15)
        )

        self.gateway_label = ctk.CTkLabel(
            status_frame,
            text="Gateway Status: Checking...",
            font=ctk.CTkFont(size=16),
            text_color="gray"
        )
        self.gateway_label.pack(
            pady=(0, 25)
        )

        # ==========================
        # Archive Statistics
        # ==========================

        stats_frame = ctk.CTkFrame(
            self,
            corner_radius=10
        )
        stats_frame.grid(
            row=3,
            column=0,
            sticky="ew",
            padx=30,
            pady=10
        )

        stats_title = ctk.CTkLabel(
            stats_frame,
            text="Archive Statistics",
            font=ctk.CTkFont(size=22, weight="bold")
        )
        stats_title.pack(
            pady=(20, 15)
        )

        stats_content = ctk.CTkFrame(
            stats_frame,
            fg_color="transparent"
        )
        stats_content.pack(
            fill="x",
            padx=30,
            pady=(0, 20)
        )

        stats_content.grid_columnconfigure(
            (0, 1, 2),
            weight=1
        )

        self.experiment_count_label = ctk.CTkLabel(
            stats_content,
            text="Experiments\n--",
            font=ctk.CTkFont(size=17),
            justify="center"
        )
        self.experiment_count_label.grid(
            row=0,
            column=0,
            padx=10,
            pady=10
        )

        self.experimental_count_label = ctk.CTkLabel(
            stats_content,
            text="Experimental Data\n--",
            font=ctk.CTkFont(size=17),
            justify="center"
        )
        self.experimental_count_label.grid(
            row=0,
            column=1,
            padx=10,
            pady=10
        )

        self.environment_count_label = ctk.CTkLabel(
            stats_content,
            text="Environment\n--",
            font=ctk.CTkFont(size=17),
            justify="center"
        )
        self.environment_count_label.grid(
            row=0,
            column=2,
            padx=10,
            pady=10
        )

        # ==========================
        # Transfer Controls
        # ==========================

        control_frame = ctk.CTkFrame(
            self,
            corner_radius=10
        )
        control_frame.grid(
            row=4,
            column=0,
            sticky="ew",
            padx=30,
            pady=10
        )

        control_title = ctk.CTkLabel(
            control_frame,
            text="Transfer Controls",
            font=ctk.CTkFont(size=24, weight="bold")
        )
        control_title.pack(
            pady=(25, 15)
        )

        button_frame = ctk.CTkFrame(
            control_frame,
            fg_color="transparent"
        )
        button_frame.pack(
            pady=(0, 25)
        )

        self.start_button = ctk.CTkButton(
            button_frame,
            text="Start Transfer",
            width=220,
            height=52,
            fg_color="#16864F",
            hover_color="#11683D",
            command=self.start_transfer
        )
        self.start_button.grid(
            row=0,
            column=0,
            padx=12
        )

        self.stop_button = ctk.CTkButton(
            button_frame,
            text="Stop Transfer",
            width=220,
            height=52,
            fg_color="#B83232",
            hover_color="#922626",
            state="disabled",
            command=self.stop_transfer
        )
        self.stop_button.grid(
            row=0,
            column=1,
            padx=12
        )

        # ==========================
        # Activity Log
        # ==========================

        log_frame = ctk.CTkFrame(
            self,
            corner_radius=10
        )
        log_frame.grid(
            row=5,
            column=0,
            sticky="nsew",
            padx=30,
            pady=(10, 25)
        )

        log_title = ctk.CTkLabel(
            log_frame,
            text="Activity Log",
            font=ctk.CTkFont(size=22, weight="bold")
        )
        log_title.pack(
            anchor="w",
            padx=20,
            pady=(20, 10)
        )

        self.log_box = ctk.CTkTextbox(
            log_frame,
            height=180,
            font=ctk.CTkFont(size=14)
        )
        self.log_box.pack(
            fill="both",
            expand=True,
            padx=20,
            pady=(0, 20)
        )

        self.log_box.configure(
            state="disabled"
        )

        # Initial messages
        self.add_log("Application ready.")
        self.add_log("Local transfer mode enabled.")
        self.add_log(
            "Gateway: http://127.0.0.1:8080"
        )
        self.add_log("Waiting for transfer to start.")

        # Begin event polling and gateway monitoring
        self.poll_events()

    # ==========================
    # Activity Log
    # ==========================

    def add_log(self, message):

        timestamp = datetime.now().strftime("%H:%M:%S")

        self.log_box.configure(
            state="normal"
        )

        self.log_box.insert(
            "end",
            f"[{timestamp}] {message}\n"
        )

        self.log_box.see("end")

        self.log_box.configure(
            state="disabled"
        )

    # ==========================
    # Start Transfer
    # ==========================

    def start_transfer(self):

        if self.controller.start():

            self.start_button.configure(
                state="disabled"
            )

            self.stop_button.configure(
                state="normal"
            )

            self.add_log(
                "Starting transfer service..."
            )

    # ==========================
    # Stop Transfer
    # ==========================

    def stop_transfer(self):

        if self.controller.stop():

            self.start_button.configure(
                state="disabled"
            )

            self.stop_button.configure(
                state="disabled"
            )

            self.add_log(
                "Waiting for transfer service to stop..."
            )

    # ==========================
    # Gateway Monitoring
    # ==========================

    def refresh_gateway(self):
        """
        Check the gateway without blocking the GUI.
        """

        if self.gateway_check_running or self.closing:
            return

        self.gateway_check_running = True

        def worker():
            try:
                self.controller.check_gateway()
            finally:
                self.gateway_check_running = False

        threading.Thread(
            target=worker,
            daemon=True,
            name="EddieGatewayMonitor"
        ).start()

    # ==========================
    # Update Archive Statistics
    # ==========================

    def update_statistics(self, counts):
        """
        Display the number of records stored
        in the remote PostgreSQL archive.
        """

        experiments = counts.get("experiment", 0)
        experimental = counts.get("experimental_data", 0)
        environment = counts.get("environment", 0)

        self.experiment_count_label.configure(
            text=f"Experiments\n{experiments}"
        )

        self.experimental_count_label.configure(
            text=f"Experimental Data\n{experimental}"
        )

        self.environment_count_label.configure(
            text=f"Environment\n{environment}"
        )

    # ==========================
    # Controller Event Polling
    # ==========================

    def poll_events(self):

        if self.closing:
            return

        for event_type, message in self.controller.get_events():

            if event_type == "status":

                self.status_label.configure(
                    text=f"Service Status: {message}"
                )

                if message == "Stopped":

                    self.start_button.configure(
                        state="normal"
                    )

                    self.stop_button.configure(
                        state="disabled"
                    )

                elif message == "Stopping":

                    self.start_button.configure(
                        state="disabled"
                    )

                    self.stop_button.configure(
                        state="disabled"
                    )

                elif message.startswith("Running"):

                    self.start_button.configure(
                        state="disabled"
                    )

                    self.stop_button.configure(
                        state="normal"
                    )

                elif message == "Starting":

                    self.start_button.configure(
                        state="disabled"
                    )

                    self.stop_button.configure(
                        state="normal"
                    )

            elif event_type == "log":

                self.add_log(message)

            elif event_type == "error":

                self.add_log(
                    f"ERROR: {message}"
                )

            elif event_type == "gateway":

                self.gateway_label.configure(
                    text=f"Gateway Status: {message}",
                    text_color=(
                        "#4CAF50"
                        if message == "Connected"
                        else "#E57373"
                    )
                )

            elif event_type == "statistics":

                self.update_statistics(message)

        # Logout only after the transfer worker stops.
        if (
            self.logout_pending
            and not self.controller.is_active()
        ):
            self.logout_pending = False
            self.on_logout()
            return

        # Check gateway every five seconds.
        now = time.monotonic()

        if (
            not self.logout_pending
            and not self.gateway_check_running
            and now - self.last_gateway_check
            >= self.gateway_check_interval_s
        ):
            self.last_gateway_check = now
            self.refresh_gateway()

        # Poll controller events every 200 milliseconds.
        self.poll_job = self.after(
            200,
            self.poll_events
        )

    # ==========================
    # Logout
    # ==========================

    def logout(self):

        if self.logout_pending:
            return

        if self.controller.is_active():

            self.logout_pending = True

            self.logout_button.configure(
                state="disabled"
            )

            self.controller.stop()

            self.add_log(
                "Stopping service before logout..."
            )

        else:
            self.on_logout()

    # ==========================
    # Cleanup
    # ==========================

    def destroy(self):

        self.closing = True

        if self.poll_job is not None:
            self.after_cancel(self.poll_job)
            self.poll_job = None

        if self.controller.is_active():
            self.controller.stop()

        super().destroy()