try {
  const theme = localStorage.getItem("sjtu-skipper.theme");
  if (theme === "light" || theme === "dark") {
    document.documentElement.style.colorScheme = theme;
    document.documentElement.dataset.theme = theme;
  }
} catch (_) {}
