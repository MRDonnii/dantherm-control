(function () {
  "use strict";
  var csrf = "";
  var $ = function (id) { return document.getElementById(id); };

  function say(text) {
    $("message").textContent = text || "";
    $("message").hidden = !text;
  }

  function call(path, body) {
    var options = { credentials: "same-origin" };
    if (body) {
      options.method = "POST";
      options.headers = { "Content-Type": "application/json", "X-CSRF-Token": csrf };
      options.body = JSON.stringify(body);
    }
    return fetch(path, options).then(function (reply) {
      return reply.json().catch(function () { return {}; }).then(function (data) {
        if (!reply.ok) throw new Error(data.error || "Det lykkedes ikke");
        return data;
      });
    });
  }

  function when(seconds) {
    return seconds ? new Date(seconds * 1000).toLocaleString("da-DK") : "";
  }

  function show(status) {
    $("state").textContent = status.enabled
      ? "Slået til. Nøgle " + status.fingerprint + "…"
      : "Slået fra. Lav en SSO-nøgle, og sæt den ind i Kontrolrum.";
    $("make-key").textContent = status.enabled ? "Ny nøgle" : "Lav SSO-nøgle";
    $("turn-off").hidden = !status.enabled;
    var used = status.last_used;
    $("used").textContent = used ? "Senest brugt " + when(used.at) + " fra " + used.ip + " som " + used.user + "." : "Ikke brugt endnu.";
    $("rejected").hidden = !status.rejected_ip;
    $("rejected").textContent = status.rejected_ip
      ? "Seneste afviste forsøg " + when(status.rejected_at) + " fra " + status.rejected_ip + " (" + status.rejected_text + ")."
      : "";
    if (document.activeElement !== $("ips")) $("ips").value = status.peers.join(", ");
    document.querySelectorAll("#users input").forEach(function (box) {
      box.checked = status.users.indexOf(box.value.toLowerCase()) !== -1;
    });
  }

  function failed(error) { say(error.message); }

  function drawUsers(users) {
    var box = $("users");
    box.textContent = "";
    users.forEach(function (user) {
      var label = document.createElement("label");
      label.className = "user";
      var input = document.createElement("input");
      input.type = "checkbox";
      input.value = user.username;
      input.addEventListener("change", function () {
        call("/api/auth/kontrolrum/user", { username: user.username, allowed: input.checked })
          .then(function (data) { show(data.status); say("Gemt."); })
          .catch(failed);
      });
      label.appendChild(input);
      label.appendChild(document.createTextNode(user.username + " må logges ind fra Kontrolrum"));
      box.appendChild(label);
    });
  }

  $("make-key").addEventListener("click", function () {
    if (!$("turn-off").hidden && !window.confirm("Lav en ny nøgle? Den gamle holder op med at virke med det samme.")) return;
    call("/api/auth/kontrolrum/key", {}).then(function (data) {
      $("key").textContent = data.key;
      $("key-box").hidden = false;
      show(data.status);
      say("");
    }).catch(failed);
  });

  $("turn-off").addEventListener("click", function () {
    if (!window.confirm("Slå login fra Kontrolrum fra? Nøglen slettes.")) return;
    call("/api/auth/kontrolrum/off", {}).then(function (data) {
      $("key-box").hidden = true;
      show(data.status);
      say("Login fra Kontrolrum er slået fra.");
    }).catch(failed);
  });

  $("peers-form").addEventListener("submit", function (event) {
    event.preventDefault();
    call("/api/auth/kontrolrum/peers", { ips: $("ips").value })
      .then(function (data) { show(data.status); say("Adressen er gemt."); })
      .catch(failed);
  });

  call("/api/auth/status").then(function (auth) {
    csrf = auth.csrf || "";
    return call("/api/users");
  }).then(function (data) {
    drawUsers(data.users || []);
    return call("/api/auth/kontrolrum/status");
  }).then(show).catch(failed);
})();
