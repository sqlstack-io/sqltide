const figure = document.querySelector('[data-showcase]');
if (figure) {
  const stories = {
    virtual: [
      ['', 'See what will change without changing the warehouse or the version serving queries.'],
      ['', 'Build the new version from retained input without switching your queries over.'],
      ['', 'Compare results before switching: the current version keeps serving queries while the new version receives live data.'],
      ['', 'Switch the view to the new version; the query stays the same and the previous version remains retained.'],
    ],
    plan: [
      ['sqt compile', 'A new filter changes order_totals. Compile validates the complete typed model offline; it does not mutate ClickHouse.'],
      ['sqt plan --select order_totals', 'Read-only plan: 2 models selected, 4 relations rebuilt, 1 replay root. The adopted input stays in place.'],
      ['sqt build --select order_totals', 'A fresh connected plan is prepared for approval. Direct build replaces order_totals and downstream order_report, then replays retained input.'],
      ['ClickHouse materialized views', 'New inserts continue through the rebuilt model chain. Replay may overlap live delivery at the inclusive cutoff; this is not exactly once.'],
    ],
    replay: [
      ['orders_input · retained history', 'The source holds rows before and after the requested UTC start. A direct rebuild preserves these source rows, not the old derived table.'],
      ['sqt plan --select order_totals\n  --start-time 2026-07-01T10:00:00Z', 'Planning checks the requested lower frontier. At build time SQLTide captures an inclusive upper cutoff; offset 102 is illustrative, not fixed by the plan.'],
      ['sqt build --select order_totals\n  --start-time 2026-07-01T10:00:00Z', 'Direct rebuild, bounded replay: older derived output is omitted. The source rows are preserved. This is not deduplication.'],
      ['Live delivery + replay · at least once', 'The attached materialized view handles new inserts while replay fills the retained window. A cutoff row may arrive via both paths; model SQL and storage own deduplication.'],
    ],
  };
  const state = { step: Number(figure.dataset.stage), playing: false, started: false, timer: null };
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const play = figure.querySelector('[data-play]');
  const render = () => {
    figure.dataset.stage = String(state.step);
    figure.classList.toggle('is-playing', state.playing);
    const [command, description] = stories[figure.dataset.showcase][state.step];
    figure.querySelector('.stage-description').textContent = description;
    if (figure.dataset.showcase === 'virtual') {
      const promoted = state.step === 3;
      const candidateBuilt = state.step > 0;
      figure.querySelector('.binding-status').textContent = promoted ? 'Now reads the new version' : 'Reads the current version';
      figure.querySelector('.version-a').textContent = promoted ? 'Previous version' : 'Current version';
      figure.querySelector('.status-a').textContent = promoted ? 'Retained after promotion' : 'Serving queries';
      figure.querySelector('.status-b').textContent = promoted ? 'Now serving queries' : candidateBuilt ? 'Receiving live data' : 'Planned · not built';
      figure.querySelector('.input-status').textContent = candidateBuilt ? 'Retained input + live data feed both versions' : 'Current version keeps receiving live data';
    } else {
      figure.querySelector('.stage-command').textContent = command;
    }
    for (const button of figure.querySelectorAll('[data-step]')) {
      button.setAttribute('aria-pressed', String(Number(button.dataset.step) === state.step));
    }
    play.textContent = figure.dataset.showcase === 'virtual' ? state.playing ? 'Pause story' : 'Play story' : state.playing ? 'Pause' : 'Play';
    play.hidden = reduced.matches;
  };
  const stop = () => {
    clearTimeout(state.timer);
    state.playing = false;
  };
  const advance = () => {
    if (!state.playing) return;
    state.step += 1;
    if (state.step === 3) stop();
    render();
    if (state.playing) state.timer = setTimeout(advance, 3600);
  };
  play.addEventListener('click', () => {
    if (state.playing) stop();
    else {
      if (state.step === 3 || !state.started) state.step = 0;
      state.started = true;
      state.playing = true;
      state.timer = setTimeout(advance, 3600);
    }
    render();
  });
  if (figure.dataset.showcase !== 'virtual') {
    figure.querySelector('[data-next]').addEventListener('click', () => {
      stop(); state.step = (state.step + 1) % 4; render();
    });
    figure.querySelector('[data-reset]').addEventListener('click', () => {
      stop(); state.step = 0; render();
    });
  }
  for (const button of figure.querySelectorAll('[data-step]')) {
    button.addEventListener('click', () => { stop(); state.started = true; state.step = Number(button.dataset.step); render(); });
  }
  reduced.addEventListener('change', () => { stop(); render(); });
  document.addEventListener('visibilitychange', () => { if (document.hidden) { stop(); render(); } });
  figure.querySelectorAll('.showcase-steps, .showcase-controls').forEach(element => { element.hidden = false; });
  render();
}
