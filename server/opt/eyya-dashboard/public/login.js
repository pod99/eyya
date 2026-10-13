const form = document.querySelector("#loginForm");
const error = document.querySelector("#loginError");
const button = form.querySelector("button");
const apiPath = (path) => new URL(path.replace(/^\//, ""), window.location.href).toString();

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  error.textContent = "";
  button.disabled = true;
  button.textContent = "Входим...";

  const data = new FormData(form);
  try {
    const response = await fetch(apiPath("api/login"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: data.get("username"),
        password: data.get("password"),
      }),
    });

    if (!response.ok) {
      error.textContent = "Проверьте логин и пароль.";
      return;
    }

    window.location.assign(new URL("./", window.location.href).toString());
  } catch {
    error.textContent = "Не удалось войти. Попробуйте еще раз.";
  } finally {
    button.disabled = false;
    button.textContent = "Войти";
  }
});
