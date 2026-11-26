// Power BI Extraction Insights: upload, follow the steps, review the results, reopen past runs.
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const CONFIG = JSON.parse($("#app-config").textContent);
  const STEPS = CONFIG.steps;
  const STEP_DATA = STEPS.indexOf("Check data"), STEP_INCORTA = STEPS.indexOf("Plan the Incorta build");
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const ERROR_HINT = "Check that the file opens in Power BI Desktop, then try again. A report on a live connection " +
                     "has no model inside: export it as a .pbit, or upload the model's own .pbix.";

  const form = $("#upload-form"), input = $("#file"), drop = $("#drop"), runBtn = $("#run-btn");
  const formError = $("#form-error"), run = $("#run"), stepsEl = $("#steps"), results = $("#results");
  let current = null;   // {id, view, downloads}
  let timer = null;

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const marked = (s) => esc(s).replace(/«([\s\S]*?)»/g, "<mark>$1</mark>");
  const unmark = (s) => String(s ?? "").replace(/[«»]/g, "");
  const num = (n) => (n === null || n === undefined || n === "" ? "–" : Number(n).toLocaleString());
  const mb = (bytes) => `${(bytes / 1048576).toFixed(1)} MB`;
  const when = (epoch) => {
    if (!epoch) return "";
    const d = new Date(epoch * 1000), p = (n) => String(n).padStart(2, "0");
    return `${p(d.getDate())}/${p(d.getMonth() + 1)}/${d.getFullYear()}, ${p(d.getHours())}:${p(d.getMinutes())}`;
  };
  const scrollTo = (el) => el.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
  const DL_ICON = '<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><path d="M7 1v8M3.5 5.5 7 9l3.5-3.5M1.5 12.5h11" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>';

  // ---- DropZone ------------------------------------------------------------------------------------
  function showFile(f) {
    formError.hidden = true;
    if (!f) return;
    $("#file-name").textContent = f.name;
    $("#file-hint").textContent = `${mb(f.size)}. Drop another file here or browse to replace it.`;
    drop.classList.add("has-file");
    runBtn.disabled = false;
  }
  input.addEventListener("change", () => showFile(input.files[0]));
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("is-over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("is-over"); }));
  drop.addEventListener("drop", (e) => {
    const f = e.dataTransfer.files[0];
    if (!f) return;
    const dt = new DataTransfer();
    dt.items.add(f);
    input.files = dt.files;
    showFile(f);
  });

  // ---- Run -----------------------------------------------------------------------------------------
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = input.files[0];
    if (!f) { fail("Choose a .pbix file first."); return; }
    const body = new FormData(form);
    runBtn.disabled = true;
    runBtn.textContent = "Creating workbook…";
    formError.hidden = true;
    results.hidden = true;
    run.hidden = false;
    $("#run-title").innerHTML = `Decoding <strong>${esc(f.name)}</strong>`;
    $("#run-title").hidden = false;
    $("#run-summary").hidden = true;
    stepsEl.hidden = false;
    const opts = { read_data: $("#opt-data").checked, incorta: $("#opt-package").checked };
    renderSteps({ step: 0, status: "queued", options: opts });
    try {
      const res = await fetch("/api/jobs", { method: "POST", body });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `The server answered ${res.status}.`);
      poll(data.id);
    } catch (ex) {
      run.hidden = true;
      fail(ex.message);
      resetButton();
    }
  });

  function fail(msg) { formError.textContent = msg; formError.hidden = false; }
  function resetButton() { runBtn.disabled = !input.files[0]; runBtn.textContent = "Create workbook"; }

  function poll(id) {
    clearTimeout(timer);
    fetch(`/api/jobs/${id}`).then((r) => {
      if (!r.ok) throw new Error(`The server answered ${r.status}.`);
      return r.json();
    }).then((job) => {
      renderSteps(job);
      if (job.status === "done") { finishRun(job); return; }
      if (job.status === "error") { resetButton(); return; }
      timer = setTimeout(() => poll(id), 400);
    }).catch(() => { timer = setTimeout(() => poll(id), 1500); });
  }

  function skipped(i, options) {
    const o = options || {};
    return (i === STEP_DATA && o.read_data === false) || (i === STEP_INCORTA && o.incorta === false);
  }

  function renderSteps(job) {
    const at = job.status === "done" ? STEPS.length : job.step || 0;
    stepsEl.innerHTML = STEPS.map((s, i) => {
      let cls = "", note = "";
      if (skipped(i, job.options) && (i < at || job.status === "done")) { cls = "skipped"; note = "Skipped"; }
      else if (i < at) cls = "done";
      else if (i === at && job.status === "error") { cls = "error"; note = `${job.error || "The step failed."} ${ERROR_HINT}`; }
      else if (i === at && job.status !== "done") { cls = "active"; note = job.message || ""; }
      return `<li class="${cls}"><span class="dot" aria-hidden="true"></span><span>${esc(s)}` +
             `${note ? `<span class="step-note">${esc(note)}</span>` : ""}</span></li>`;
    }).join("");
  }

  function finishRun(job) {
    resetButton();
    const done = STEPS.filter((_, i) => !skipped(i, job.options)).length;
    const secs = job.view && job.view.seconds != null ? Number(job.view.seconds).toFixed(1) : "–";
    $("#run-summary-text").innerHTML = `<strong>Decoded in ${esc(secs)} s</strong>, ${done} of ${STEPS.length} steps done` +
      (done < STEPS.length ? ` (${STEPS.length - done} skipped by the options).` : ".");
    $("#run-title").hidden = true;
    stepsEl.hidden = true;
    $("#steps-toggle").setAttribute("aria-expanded", "false");
    $("#steps-toggle").textContent = "Show steps";
    $("#run-summary").hidden = false;
    showResult(job);
    loadHistory();
  }

  $("#steps-toggle").addEventListener("click", (e) => {
    const open = e.currentTarget.getAttribute("aria-expanded") === "true";
    e.currentTarget.setAttribute("aria-expanded", String(!open));
    e.currentTarget.textContent = open ? "Show steps" : "Hide steps";
    stepsEl.hidden = open;
  });

  // ---- Results ---------------------------------------------------------------------------------------
  function showResult(job, scroll = true) {
    if (!job.view) {
      fail("This run was made by an older version and can't be shown here. Its downloads still work.");
      return;
    }
    current = { id: job.id, view: job.view, downloads: job.downloads || {} };
    const v = current.view, c = v.counts, review = c.measures - c.translated;
    $("#res-file").textContent = v.file;
    $("#result-meta").textContent = c.queries != null
      ? `Spark SQL for Incorta. ${num(c.queries)} insight queries ran on the saved data.`
      : "Spark SQL for Incorta. Insight queries weren't test-run: tick “Read the data saved in the file” to test them.";
    renderDownloads();
    const w = v.warnings || [];
    $("#warnings").hidden = !w.length;
    $("#warnings").innerHTML = w.length ? `<strong>${w.length === 1 ? "1 warning" : `${w.length} warnings`}</strong><ul>${
      w.slice(0, 8).map((x) => `<li>${esc(x)}</li>`).join("")}</ul>${w.length > 8 ? `<p>…and ${w.length - 8} more in the JSON.</p>` : ""}` : "";

    $("#cov-translated").textContent = num(c.translated);
    $("#cov-measures").textContent = num(c.measures);
    $("#leg-translated").textContent = num(c.translated);
    $("#leg-review").textContent = num(review);
    const bar = $("#bar");
    bar.innerHTML = c.measures ? `<div class="ok" style="flex:${c.translated}"></div><div class="todo" style="flex:${review}"></div>` : "";
    bar.setAttribute("aria-label", `${c.translated} of ${c.measures} measures translated`);
    const stats = [[c.tables, "schema tables"], [c.mvs, "MVs to build"], [c.joins, "joins"], [c.tabs, "dashboard tabs"],
                   [c.insights, "insights"], [c.findings, `findings, ${num(c.high)} high`]];
    $("#stats").innerHTML = stats.map(([n, l]) => `<div class="stat"><b>${num(n)}</b><span>${esc(l)}</span></div>`).join("");
    renderTabs();
    results.hidden = false;
    if (scroll) scrollTo(results);
  }

  function renderDownloads() {
    const d = current.downloads;
    const items = d.package
      ? [["Incorta package (.zip)", d.package, true], ["Workbook (.xlsx)", d.workbook, false], ["JSON", d.json, false]]
      : [["Workbook (.xlsx)", d.workbook, true], ["JSON", d.json, false]];
    $("#downloads").innerHTML = items.filter(([, href]) => href).map(([label, href, primary]) =>
      `<a class="btn ${primary ? "btn-primary" : ""}" href="${esc(href)}" download>${DL_ICON}${esc(label)}</a>`).join("");
  }

  function table(head, rows, empty) {
    if (!rows.length) return `<p class="empty">${esc(empty)}</p>`;
    return `<div class="table-wrap"><table><thead><tr>${head.map(([h, cls]) => `<th${cls ? ` class="${cls}"` : ""}>${esc(h)}</th>`).join("")}</tr></thead>` +
           `<tbody>${rows.join("")}</tbody></table></div>`;
  }

  function renderReview() {
    const v = current.view;
    const groups = v.groups.map((g, gi) => `
      <details class="group" ${gi === 0 ? "open" : ""}>
        <summary>
          <div><div class="group-title"><code>${esc(g.pattern)}</code></div><div class="group-why">${esc(g.why)}</div></div>
          <span class="group-n" aria-label="${g.measures.length} measures">${g.measures.length}</span>
          <span class="chev" aria-hidden="true"></span>
        </summary>
        <p class="group-fix">${esc(g.fix)}</p>
        ${g.measures.map((m, mi) => `
          <details class="measure">
            <summary><span class="measure-name">[${esc(m.name)}]</span><span class="status ${m.done ? "is-done" : ""}">${m.done ? "Done" : "Draft SQL"}</span></summary>
            <div class="split">
              <div><p class="split-label">DAX in Power BI</p><pre>${marked(m.dax)}</pre></div>
              <div><p class="split-label">Draft Spark SQL</p><pre>${marked(m.sql)}</pre></div>
            </div>
            <p class="measure-reason">Translator: ${esc(m.reason)}</p>
            <div class="measure-actions">
              <button type="button" class="btn" data-copy data-g="${gi}" data-m="${mi}">Copy SQL</button>
              <button type="button" class="btn" data-done data-g="${gi}" data-m="${mi}">${m.done ? "Mark as to do" : "Mark as done"}</button>
            </div>
          </details>`).join("")}
      </details>`).join("");

    const sevName = (s) => s.charAt(0).toUpperCase() + s.slice(1);
    const detail = (d) => {
      const s = String(d ?? "");
      if (s.length <= 240) return `<span class="detail">${esc(s)}</span>`;
      const cut = s.slice(0, 200).replace(/\s+\S*$/, "");
      return `<details class="more"><summary><span class="detail">${esc(cut)}…</span> <span class="more-link">Show all</span></summary>` +
             `<span class="detail">${esc(s)}</span></details>`;
    };
    const rows = v.findings.map((f) => `<tr><td><span class="sev sev-${esc(f.severity)}">${esc(sevName(f.severity))}</span></td>` +
      `<td>${esc(f.area)}</td><td><strong>${esc(f.title)}</strong><br>${detail(f.detail)}</td><td>${esc(f.rec)}</td></tr>`);
    const intro = v.groups.length
      ? `<div class="review-intro"><h3>Measures to write by hand</h3><p class="help">Highlighted parts are what the rules couldn't translate.</p></div>${groups}`
      : `<div class="review-intro"><h3>Measures to write by hand</h3></div><p class="empty">${v.otherReviewCount ? "" : "Every measure was translated. Nothing to write by hand."}</p>`;
    const other = v.otherReviewCount
      ? `<p class="help" style="margin-top:12px">${v.otherReviewCount} more ${v.otherReviewCount === 1 ? "measure" : "measures"} with other patterns ${v.otherReviewCount === 1 ? "is" : "are"} listed on the Measures sheet of the workbook.</p>` : "";
    return `${intro}${other}<div class="other"><h3>Other findings</h3>${table(
      [["Severity"], ["Area"], ["Finding"], ["What to do"]], rows, "No other findings.")}</div>`;
  }

  function renderBuildPlan() {
    const v = current.view;
    if (!v.package) return `<p class="empty">The Incorta package wasn't built for this run. Tick “Build the Incorta package” and run the file again to see the MVs.</p>`;
    const rows = v.mvs.map((m) => `<tr><td class="code mv-name">${esc(m.name)}</td><td>${esc(m.from)}</td><td>${esc(m.feeds)}</td><td>${m.ready
      ? `<span class="pill pill-ok">Ready to build</span>`
      : `<span class="pill pill-todo">Needs review</span><br><span class="help">Blocked by <code>${esc(m.note)}</code></span>`}</td></tr>`);
    return table([["MV"], ["Builds from"], ["Feeds dashboard tabs"], ["Status"]], rows, "This report needs no MVs.") +
      (v.mvs.length ? `<p class="help" style="margin-top:12px">${v.mvs.length} MVs in build order${v.schema ? `, in the schema <code>${esc(v.schema)}</code>` : ""}. The full build plan, with joins and formulas, is in the Incorta package.</p>` : "");
  }

  function renderInsights() {
    const v = current.view;
    const rows = v.insights.map((i) => `<tr><td>${esc(i.tab)}</td><td>${esc(i.name)}</td><td>${esc(i.type)}</td>` +
      `<td class="num">${i.rows != null ? num(i.rows) : (i.run ? esc(i.run) : "Not run")}</td>` +
      `<td><span class="pill ${i.ok ? "pill-ok" : "pill-todo"}">${i.ok ? "Translated" : "Needs review"}</span></td></tr>`);
    return table([["Tab"], ["Insight"], ["Visual"], ["Rows returned", "num"], ["SQL"]], rows, "The report has no data insights.");
  }

  function renderMeasures() {
    const v = current.view;
    const order = (m) => (m.status === "Needs review" ? 0 : 1);
    const rows = [...v.measures].sort((a, b) => order(a) - order(b)).map((m) => `<tr><td class="code">[${esc(m.name)}]</td>` +
      `<td>${esc(m.table)}</td><td>${esc(m.scope)}</td><td>${m.pattern ? `<code>${esc(m.pattern)}</code>` : "–"}</td>` +
      `<td><span class="pill ${m.status === "Translated" ? "pill-ok" : "pill-todo"}">${esc(m.status)}</span></td></tr>`);
    return table([["Measure"], ["Table"], ["Scope"], ["Blocked by"], ["SQL"]], rows, "The model has no measures.");
  }

  function renderSources() {
    const rows = current.view.sources.map((s) => `<tr><td>${esc(s.name)}</td><td>${esc(s.kind)}</td>` +
      `<td class="code">${esc(s.detail)}</td><td class="num">${num(s.steps)}</td></tr>`);
    return table([["Query"], ["Source"], ["Object"], ["Applied steps", "num"]], rows, "No Power Query sources.");
  }

  const TABS = [
    { id: "review", label: "To review", count: (v) => v.counts.measures - v.counts.translated, render: renderReview },
    { id: "build", label: "Build plan", count: (v) => (v.package ? v.mvs.length : "–"), render: renderBuildPlan },
    { id: "insights", label: "Insights", count: (v) => v.insights.length, render: renderInsights },
    { id: "measures", label: "Measures", count: (v) => v.counts.measures, render: renderMeasures },
    { id: "sources", label: "Sources", count: (v) => v.sources.length, render: renderSources },
  ];

  function renderTabs() {
    const v = current.view;
    $("#tabs").innerHTML = TABS.map((t, i) =>
      `<button type="button" class="tab" role="tab" id="tab-${t.id}" aria-controls="panel-${t.id}" aria-selected="${i === 0}" tabindex="${i === 0 ? 0 : -1}">` +
      `${esc(t.label)}<span class="count">${num(t.count(v))}</span></button>`).join("");
    $("#panels").innerHTML = TABS.map((t, i) =>
      `<div class="tabpanel" role="tabpanel" id="panel-${t.id}" aria-labelledby="tab-${t.id}" tabindex="0" ${i === 0 ? "" : "hidden"}>${t.render()}</div>`).join("");
    const tabs = $$(".tab");
    const select = (tab, focus = true) => {
      tabs.forEach((t) => {
        const on = t === tab;
        t.setAttribute("aria-selected", String(on));
        t.tabIndex = on ? 0 : -1;
        $("#" + t.getAttribute("aria-controls")).hidden = !on;
      });
      if (focus) tab.focus();
    };
    tabs.forEach((t, i) => {
      t.addEventListener("click", () => select(t));
      t.addEventListener("keydown", (e) => {
        const to = { ArrowRight: (i + 1) % tabs.length, ArrowLeft: (i - 1 + tabs.length) % tabs.length, Home: 0, End: tabs.length - 1 }[e.key];
        if (to !== undefined) { e.preventDefault(); select(tabs[to]); }
      });
    });
  }

  // ---- Copy SQL and Mark as done -----------------------------------------------------------------------
  document.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-copy],[data-done]");
    if (!b || !current) return;
    const m = current.view.groups[+b.dataset.g].measures[+b.dataset.m];
    if (b.hasAttribute("data-copy")) {
      const text = unmark(m.sql);
      try { await navigator.clipboard.writeText(text); }
      catch {
        const ta = Object.assign(document.createElement("textarea"), { value: text });
        document.body.append(ta); ta.select(); document.execCommand("copy"); ta.remove();
      }
      b.textContent = "Copied";
      setTimeout(() => { b.textContent = "Copy SQL"; }, 1500);
      return;
    }
    const want = !m.done;
    b.disabled = true;
    try {
      const res = await fetch(`/api/jobs/${current.id}/done`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ measure: m.name, done: want }) });
      if (!res.ok) throw new Error();
      m.done = (await res.json()).done;
      const st = $(".status", b.closest(".measure"));
      st.textContent = m.done ? "Done" : "Draft SQL";
      st.classList.toggle("is-done", m.done);
      b.textContent = m.done ? "Mark as to do" : "Mark as done";
    } catch {
      b.textContent = "Not saved, try again";
      setTimeout(() => { b.textContent = m.done ? "Mark as to do" : "Mark as done"; }, 2000);
    } finally {
      b.disabled = false;
    }
  });

  // ---- History ----------------------------------------------------------------------------------------
  function loadHistory() {
    fetch("/api/history").then((r) => r.json()).then((files) => {
      const el = $("#history");
      if (!files.length) { el.innerHTML = '<p class="empty">No workbooks yet. Created workbooks appear here.</p>'; return; }
      el.innerHTML = files.map((h) => `
        <div class="hist-file">
          <div class="hist-top"><span class="hist-name">${esc(h.file)}</span><span class="help">${h.runs.length > 1 ? `${h.runs.length} runs` : "1 run"}</span></div>
          <ul class="hist-runs">${h.runs.map((r) => `
            <li class="${current && current.id === r.id ? "is-current" : ""}"><span>${esc(when(r.created))}</span>
              <span>${esc(r.summary)}${r.diff ? `. <span class="diff">${esc(r.diff)}</span>` : ""}</span>
              <span class="hist-links">${r.viewable ? `<button type="button" class="linkbtn" data-open="${esc(r.id)}">Open</button>` : ""}` +
              `<a href="${esc(r.downloads.workbook)}" download>Workbook</a><a href="${esc(r.downloads.json)}" download>JSON</a>` +
              `${r.downloads.package ? `<a href="${esc(r.downloads.package)}" download>Package</a>` : ""}</span></li>`).join("")}
          </ul>
        </div>`).join("");
    }).catch(() => {});
  }

  $("#history").addEventListener("click", (e) => {
    const b = e.target.closest("[data-open]");
    if (!b) return;
    fetch(`/api/jobs/${b.dataset.open}`).then((r) => r.json()).then((job) => {
      formError.hidden = true;
      run.hidden = true;
      showResult(job);
      loadHistory();
    }).catch(() => fail("That run could not be opened."));
  });

  loadHistory();
})();
