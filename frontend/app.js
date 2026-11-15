/* ChangeFlow frontend. Vanilla JS, no build step. Talks to the HTTP API via fetch. */
(() => {
  const API = (window.CHANGEFLOW_CONFIG && window.CHANGEFLOW_CONFIG.apiBase) || "";
  if (!API) {
    document.getElementById("submit-msg").textContent =
      "config.js is missing an apiBase. Run scripts/deploy-frontend.sh after sam deploy.";
    document.getElementById("submit-msg").className = "msg error";
  }

  let selectedId = null;
  let currentApproverName = "";
  let currentDecisionComment = "";
  let lastRequestStatus = null;
  let lastRequestId = null;
  let focusedFieldId = null;

  const $ = (sel) => document.querySelector(sel);
  const form = $("#submit-form");
  const msg = $("#submit-msg");
  const tbody = $("#request-table tbody");

  async function api(path, options = {}) {
    const res = await fetch(API + path, {
      headers: { "content-type": "application/json" },
      ...options,
    });
    let body = {};
    try { body = await res.json(); } catch (e) { /* empty body */ }
    if (!res.ok) throw new Error(body.error || ("HTTP " + res.status));
    return body;
  }

  function fmtTime(iso) {
    if (!iso) return "-";
    const clean = iso.replace(/\+\d{2}:\d{2}Z$/, 'Z');
    const d = new Date(clean);
    if (isNaN(d.getTime())) return "-";
    return d.toLocaleString([], { dateStyle: "short", timeStyle: "medium" });
  }

  function pill(status) {
    return `<span class="pill ${status}">${status}</span>`;
  }

  async function loadList() {
    try {
      const data = await api("/requests");
      const rows = (data.requests || []).map((r) => `
        <tr data-id="${r.requestId}" class="${r.requestId === selectedId ? "selected" : ""}">
          <td>${r.requestId}</td>
          <td>${escapeHtml(r.title)}</td>
          <td>${escapeHtml(r.partNumber)}</td>
          <td>${r.impact}</td>
          <td>${pill(r.status)}</td>
          <td>${fmtTime(r.submittedAt)}</td>
        </tr>`).join("");
      tbody.innerHTML = rows;
      $("#empty-note").classList.toggle("hidden", (data.count || 0) > 0);
    } catch (err) {
      msg.textContent = "Could not load requests: " + err.message;
      msg.className = "msg error";
    }
  }

  async function loadDetail(id) {
    try {
      const data = await api("/requests/" + encodeURIComponent(id));
      renderDetail(data.request);
    } catch (err) {
      showMsg("Could not load request: " + err.message, "error");
    }
  }

  function renderDetail(r) {
    const card = $("#detail-card");
    card.classList.remove("hidden");
    
    // Reset decision inputs if not awaiting approval
    if (r.status !== "AWAITING_APPROVAL") {
      currentApproverName = "";
      currentDecisionComment = "";
    }
    
    // Store current status for polling optimization
    lastRequestStatus = r.status;
    lastRequestId = r.requestId;
    
    const timeline = (r.history || []).map((h) => `
      <li>
        <span class="ev">${h.event}</span>
        <span class="ts">${fmtTime(h.ts)}</span>
        <span>by ${escapeHtml(h.by || "system")}</span>
        ${h.comment ? `<div class="cm">&ldquo;${escapeHtml(h.comment)}&rdquo;</div>` : ""}
      </li>`).join("");

    const decisionBox = r.status === "AWAITING_APPROVAL" ? `
      <div class="decision-box">
        <div class="row">
          <label>Approver name
            <input id="approver-name" placeholder="e.g. Engineering Lead" value="${escapeHtml(currentApproverName)}" />
          </label>
          <label>Comment
            <input id="decision-comment" placeholder="optional" value="${escapeHtml(currentDecisionComment)}" />
          </label>
        </div>
        <div class="decision-buttons">
          <button class="reject" data-decision="reject">Reject</button>
          <button class="approve" data-decision="approve">Approve</button>
        </div>
      </div>` : "";

    $("#detail-body").innerHTML = `
      <div class="meta">
        <div><span class="k">Status</span>${pill(r.status)}</div>
        <div><span class="k">Part</span>${escapeHtml(r.partNumber)}</div>
        <div><span class="k">Impact</span>${r.impact}</div>
        <div><span class="k">Requested by</span>${escapeHtml(r.requestedBy)}</div>
        <div><span class="k">Submitted</span>${fmtTime(r.submittedAt)}</div>
        <div><span class="k">Decided by</span>${escapeHtml(r.decidedBy || "-")}</div>
      </div>
      <div class="description">${escapeHtml(r.description)}</div>
      <a class="snapshot-link" href="#" id="snapshot-link">View frozen snapshot (S3, presigned) &rarr;</a>
      <ul class="timeline">${timeline}</ul>
      ${decisionBox}
    `;

    $("#snapshot-link").addEventListener("click", async (e) => {
      e.preventDefault();
      try {
        const data = await api(`/requests/${encodeURIComponent(r.requestId)}/snapshot`);
        window.open(data.url, "_blank");
      } catch (err) {
        showMsg("Snapshot unavailable: " + err.message, "error");
      }
    });

    // Save input values to state and manage focus
    const approverInput = $("#approver-name");
    const commentInput = $("#decision-comment");
    if (approverInput) {
      approverInput.addEventListener("input", (e) => { currentApproverName = e.target.value; });
      approverInput.addEventListener("focus", () => { focusedFieldId = "approver-name"; });
      approverInput.addEventListener("blur", () => { focusedFieldId = null; });
    }
    if (commentInput) {
      commentInput.addEventListener("input", (e) => { currentDecisionComment = e.target.value; });
      commentInput.addEventListener("focus", () => { focusedFieldId = "decision-comment"; });
      commentInput.addEventListener("blur", () => { focusedFieldId = null; });
    }
    
    // Restore focus after render
    if (focusedFieldId) {
      const focusedEl = $(`#${focusedFieldId}`);
      if (focusedEl) {
        // Use setTimeout to ensure the element is in the DOM
        setTimeout(() => focusedEl.focus(), 10);
      }
    }

    document.querySelectorAll(".decision-buttons button").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const decision = btn.dataset.decision;
        const by = currentApproverName.trim() || "approver";
        const comment = currentDecisionComment.trim() || null;
        try {
          await api(`/requests/${encodeURIComponent(r.requestId)}/decision`, {
            method: "POST",
            body: JSON.stringify({ decision, by, comment }),
          });
          showMsg(decision === "approve" ? "Approved. Workflow is executing the change." : "Rejected. Workflow is closing the request.", "ok");
          await Promise.all([loadList(), loadDetail(r.requestId)]);
        } catch (err) {
          showMsg(err.message, "error");
        }
      });
    });
  }

  function showMsg(text, kind) {
    msg.textContent = text;
    msg.className = "msg " + (kind || "");
  }

  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const data = Object.fromEntries(new FormData(form).entries());
    try {
      const res = await api("/requests", { method: "POST", body: JSON.stringify(data) });
      form.reset();
      showMsg(`Submitted ${res.request.requestId}. Workflow is validating.`, "ok");
      selectedId = res.request.requestId;
      await Promise.all([loadList(), loadDetail(selectedId)]);
    } catch (err) {
      showMsg(err.message, "error");
    }
  });

  tbody.addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-id]");
    if (!tr) return;
    selectedId = tr.dataset.id;
    document.querySelectorAll("tbody tr").forEach((el) => el.classList.remove("selected"));
    tr.classList.add("selected");
    loadDetail(selectedId);
  });

  $("#refresh").addEventListener("click", () => { loadList(); if (selectedId) loadDetail(selectedId); });
  $("#close-detail").addEventListener("click", () => {
    $("#detail-card").classList.add("hidden");
    selectedId = null;
  });

  loadList();
  // Light polling so the demo shows state transitions without manual refresh.
  setInterval(() => {
    loadList();
    if (selectedId && !focusedFieldId) {
      // Only reload detail if status changed and no field is focused (avoids resetting form focus)
      api("/requests/" + encodeURIComponent(selectedId))
        .then((data) => {
          if (data.request && (data.request.status !== lastRequestStatus || data.request.requestId !== lastRequestId)) {
            loadDetail(selectedId);
          }
        })
        .catch(() => {});
    }
  }, 5000);
})();
