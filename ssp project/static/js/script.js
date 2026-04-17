(function () {
  // ==========================================
  // Dark Mode Theme Management
  // ==========================================
  const themeStorageKey = "ssp-theme";
  const root = document.documentElement;
  const prefersDark =
    window.matchMedia &&
    window.matchMedia("(prefers-color-scheme: dark)").matches;

  function getCurrentTheme() {
    return root.dataset.theme === "dark" ? "dark" : "light";
  }

  function updateThemeButtons(theme) {
    document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
      const icon = button.querySelector("i");
      const label = button.querySelector("span");
      const isDark = theme === "dark";

      if (icon) {
        icon.className = isDark ? "fa-solid fa-sun" : "fa-solid fa-moon";
      }

      if (label) {
        label.textContent = isDark ? "Light mode" : "Dark mode";
      }

      button.setAttribute("aria-pressed", String(isDark));
    });
  }

  function setTheme(theme) {
    root.dataset.theme = theme;
    localStorage.setItem(themeStorageKey, theme);
    updateThemeButtons(theme);
    document.dispatchEvent(
      new CustomEvent("themechange", {
        detail: { theme },
      }),
    );
  }

  const savedTheme = localStorage.getItem(themeStorageKey);
  root.dataset.theme = savedTheme || (prefersDark ? "dark" : "light");
  updateThemeButtons(getCurrentTheme());

  // ==========================================
  // Active Page Navigation Highlighting
  // ==========================================
  function updateActiveNavLink() {
    const currentUrl = window.location.pathname;
    document.querySelectorAll(".nav-link").forEach((link) => {
      const href = link.getAttribute("href");
      const isActive = href && (
        href === currentUrl || 
        (currentUrl === "/" && href === "/dashboard") ||
        currentUrl.startsWith(href) && href !== "/"
      );
      
      link.classList.toggle("active", isActive);
      link.setAttribute("aria-current", isActive ? "page" : "false");
    });
  }

  // Call on page load
  updateActiveNavLink();

  // ==========================================
  // Form Enhancement
  // ==========================================
  function enhanceForms() {
    const inputs = document.querySelectorAll("input, textarea");
    
    inputs.forEach((input) => {
      // Add visual feedback on input
      input.addEventListener("focus", function() {
        this.parentElement?.classList.add("input-focused");
      });

      input.addEventListener("blur", function() {
        this.parentElement?.classList.remove("input-focused");
      });

      // Prevent double submit
      const form = input.closest("form");
      if (form) {
        form.addEventListener("submit", function() {
          const submitBtn = this.querySelector("button[type='submit']");
          if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.style.opacity = "0.6";
            submitBtn.style.cursor = "not-allowed";
          }
        });
      }
    });
  }

  enhanceForms();

  // ==========================================
  // Sidebar Management
  // ==========================================
  function manageSidebar() {
    const sidebar = document.getElementById("appSidebar");
    
    // Close sidebar when clicking on a nav link (for mobile)
    if (sidebar) {
      sidebar.querySelectorAll(".nav-link").forEach((link) => {
        link.addEventListener("click", () => {
          if (window.innerWidth <= 1024) {
            document.body.classList.remove("sidebar-open");
          }
        });
      });
    }
  }

  manageSidebar();

  // ==========================================
  // Global Event Listeners
  // ==========================================
  document.addEventListener("click", (event) => {
    // Theme toggle
    const themeToggle = event.target.closest("[data-theme-toggle]");
    if (themeToggle) {
      setTheme(getCurrentTheme() === "dark" ? "light" : "dark");
      return;
    }

    // Sidebar open
    const sidebarOpenButton = event.target.closest("[data-sidebar-open]");
    if (sidebarOpenButton) {
      document.body.classList.add("sidebar-open");
      return;
    }

    // Sidebar close
    const sidebarCloseButton = event.target.closest("[data-sidebar-close]");
    if (sidebarCloseButton) {
      document.body.classList.remove("sidebar-open");
    }
  });

  // ==========================================
  // Keyboard Navigation
  // ==========================================
  window.addEventListener("keydown", (event) => {
    // Close sidebar with Escape
    if (event.key === "Escape") {
      document.body.classList.remove("sidebar-open");
    }
  });

  // ==========================================
  // Responsive Behavior
  // ==========================================
  window.addEventListener("resize", () => {
    if (window.innerWidth > 1024) {
      document.body.classList.remove("sidebar-open");
    }
  });

  // ==========================================
  // Loading State Management
  // ==========================================
  window.addEventListener("beforeunload", () => {
    document.body.style.opacity = "0.95";
  });

  window.addEventListener("load", () => {
    document.body.style.opacity = "1";
    // Ensure active link is set after page loads
    updateActiveNavLink();
    // Load and display notifications on page load
    loadAndDisplayNotifications();
  });

  // ==========================================
  // Notification System
  // ==========================================
  function createToastContainer() {
    let container = document.getElementById("notification-container");
    if (!container) {
      container = document.createElement("div");
      container.id = "notification-container";
      container.style.cssText = `
        position: fixed;
        top: 20px;
        right: 20px;
        z-index: 10000;
        display: flex;
        flex-direction: column;
        gap: 10px;
        pointer-events: none;
      `;
      document.body.appendChild(container);
    }
    return container;
  }

  function showToastNotification(message, type = "success", duration = 4000) {
    const container = createToastContainer();
    const toast = document.createElement("div");
    
    const colors = {
      success: "#10b981",
      error: "#ef4444",
      warning: "#f59e0b",
      info: "#3b82f6",
    };
    
    const icons = {
      success: "fa-check-circle",
      error: "fa-exclamation-circle",
      warning: "fa-exclamation-triangle",
      info: "fa-info-circle",
    };
    
    toast.style.cssText = `
      background: ${colors[type] || colors.info};
      color: white;
      padding: 16px 20px;
      border-radius: 8px;
      display: flex;
      align-items: center;
      gap: 12px;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
      animation: slideIn 0.3s ease-out;
      pointer-events: auto;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
      font-size: 14px;
      font-weight: 500;
      max-width: 400px;
    `;
    
    const icon = document.createElement("i");
    icon.className = `fa-solid ${icons[type] || icons.info}`;
    icon.style.cssText = "flex-shrink: 0; font-size: 18px;";
    
    const text = document.createElement("span");
    text.textContent = message;
    text.style.cssText = "flex: 1;";
    
    toast.appendChild(icon);
    toast.appendChild(text);
    container.appendChild(toast);
    
    setTimeout(() => {
      toast.style.animation = "slideOut 0.3s ease-out";
      setTimeout(() => toast.remove(), 300);
    }, duration);
  }

  function loadAndDisplayNotifications() {
    // Add CSS animation styles if not already present
    if (!document.getElementById("notification-styles")) {
      const style = document.createElement("style");
      style.id = "notification-styles";
      style.textContent = `
        @keyframes slideIn {
          from {
            transform: translateX(400px);
            opacity: 0;
          }
          to {
            transform: translateX(0);
            opacity: 1;
          }
        }
        @keyframes slideOut {
          from {
            transform: translateX(0);
            opacity: 1;
          }
          to {
            transform: translateX(400px);
            opacity: 0;
          }
        }
      `;
      document.head.appendChild(style);
    }

    // Fetch unread notifications
    fetch("/notifications")
      .then((response) => response.json())
      .then((data) => {
        if (data.notifications && data.notifications.length > 0) {
          // Show only the most recent unread notification
          const unreadNotifications = data.notifications.filter((n) => !n.is_read);
          if (unreadNotifications.length > 0) {
            const notification = unreadNotifications[0];
            showToastNotification(notification.message, "success", 5000);
            
            // Mark as read after showing
            fetch(`/notifications/mark_as_read/${notification.id}`, {
              method: "POST",
            }).catch((err) => console.log("Could not mark notification as read:", err));
          }
        }
      })
      .catch((err) => console.log("Could not load notifications:", err));
  }

  // Debug: Log theme changes (remove in production)
  document.addEventListener("themechange", (e) => {
    console.log("Theme changed to:", e.detail.theme);
  });
})();
