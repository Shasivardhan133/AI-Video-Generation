(() => {
  "use strict";

  const body = document.body;
  const sidebarToggle = document.querySelector("[data-sidebar-toggle]");
  const sidebarClose = document.querySelector("[data-sidebar-close]");
  const sidebarBackdrop = document.querySelector("[data-sidebar-backdrop]");
  const newVideoButton = document.querySelector("[data-new-video]");
  const prompt = document.querySelector("[data-prompt]");
  const composerForm = document.querySelector("[data-composer-form]");
  const composer = composerForm?.querySelector(".composer");
  const imageInput = document.querySelector("[data-image-input]");
  const imagePreview = document.querySelector("[data-image-preview]");
  const previewImage = document.querySelector("[data-preview-image]");
  const previewName = document.querySelector("[data-preview-name]");
  const removeImage = document.querySelector("[data-remove-image]");
  const formMessage = document.querySelector("[data-form-message]");
  const suggestionCards = document.querySelectorAll("[data-suggestion]");
  const composerPlus = document.querySelector("[data-composer-plus]");
  const settingInputs = document.querySelectorAll("[data-setting]");
  const panelToggles = document.querySelectorAll("[data-panel-toggle]");
  const panelSuggestions = document.querySelectorAll("[data-panel-suggestion]");
  const secondaryToggles = document.querySelectorAll("[data-secondary-toggle]");
  const maxImageSize = 25 * 1024 * 1024; // Matches the API limit (25 MB = 25 MiB).
  let previewUrl = null;
  const generationSettings = {
    aspectRatio: document.querySelector("[data-setting='aspectRatio']:checked")?.value || "landscape",
    frameCount: document.querySelector("[data-setting='frameCount']:checked")?.value || "81",
    visualStyle: document.querySelector("[data-setting='visualStyle']:checked")?.value || "cinematic"
  };
  const historyList = document.querySelector("[data-history-list]");
  const historyEmpty = document.querySelector("[data-history-empty]");
  const libraryList = document.querySelector("[data-library-list]");
  const libraryEmpty = document.querySelector("[data-library-empty]");
  const pinnedList = document.querySelector("[data-pinned-list]");
  const pinnedEmpty = document.querySelector("[data-pinned-empty]");
  const pinStatus = document.querySelector("[data-pin-status]");
  const submitButton = document.querySelector(".generate-button");
  const previewToggle = document.querySelector("[data-preview-toggle]");
  const previewPanel = document.querySelector("[data-preview-panel]");
  const previewHeading = document.getElementById("video-preview-heading");
  const previewStatus = document.querySelector("[data-preview-status]");
  const previewClose = document.querySelector("[data-preview-close]");
  const previewMock = document.querySelector("[data-preview-mock]");
  const previewState = document.querySelector("[data-preview-state]");
  const previewStateIcon = document.querySelector("[data-preview-state-icon]");
  const previewStateHeading = document.querySelector("[data-preview-state-heading]");
  const previewStateText = document.querySelector("[data-preview-state-text]");
  const previewRetry = document.querySelector("[data-preview-retry]");
  const previewVideo = document.querySelector("[data-preview-video]");
  const previewCaption = document.querySelector("[data-preview-caption]");
  const previewActions = document.querySelector("[data-preview-actions]");
  const previewDownload = document.querySelector("[data-preview-download]");

  // ---------------------------------------------------------------------------
  // API adapter for the Django backend (/api/v1/). The page is served by Django,
  // so requests are same-origin and carry the session cookie.
  // ---------------------------------------------------------------------------
  const API_BASE = "/api/v1";
  const POLL_INTERVAL_MS = 3000;
  // Used only until GET /profiles/ answers, or if it fails. They match the
  // backend's default profiles, and no model_profile is sent with them.
  const FALLBACK_SIZES = { landscape: [832, 480], portrait: [480, 832] };
  const MODE_LABELS = { text_to_video: "Text to video", image_to_video: "Image to video" };
  const pollers = new Map();

  // Django requires a CSRF token on POST requests. It is fetched once from the
  // API (which also sets the csrftoken cookie) and sent in the X-CSRFToken header.
  let csrfToken = null;
  const getCsrfToken = async (refresh = false) => {
    if (!csrfToken || refresh) {
      let response;
      try {
        response = await fetch(`${API_BASE}/csrf/`, { credentials: "same-origin" });
      } catch (error) {
        throw new Error("Cannot reach the server. Is Django running?");
      }
      const data = await response.json().catch(() => null);
      // Never send a missing token ("undefined"); show the server's own error instead.
      if (!response.ok || typeof data?.csrf_token !== "string") {
        throw new Error(data?.error?.message || `Could not get a security token (${response.status}). Reload the page.`);
      }
      csrfToken = data.csrf_token;
    }
    return csrfToken;
  };

  const apiRequest = async (path, options = {}, isRetry = false) => {
    const isWrite = (options.method || "GET").toUpperCase() !== "GET";
    // Outside the try below, so a token error keeps its own message.
    const headers = isWrite
      ? { ...options.headers, "X-CSRFToken": await getCsrfToken(isRetry) }
      : options.headers;
    let response;
    try {
      response = await fetch(`${API_BASE}${path}`, { credentials: "same-origin", ...options, headers });
    } catch (error) {
      throw new Error("Cannot reach the server. Is Django running?");
    }
    let data = null;
    try {
      data = await response.json();
    } catch (error) {
      data = null;
    }
    // A stale token (for example after cookies were cleared) is refreshed once.
    if (response.status === 403 && isWrite && !isRetry && data?.error?.message?.startsWith("CSRF Failed")) {
      return apiRequest(path, options, true);
    }
    if (!response.ok) {
      const error = new Error(data?.error?.message || `Request failed (${response.status}).`);
      error.status = response.status;
      error.code = data?.error?.code || null;
      throw error;
    }
    return data;
  };

  // Generation profiles. The API lists the settings it accepts; for each mode
  // the page uses the profile the backend marks as default. Until that list has
  // loaded, or if it cannot be loaded, the built-in values above are used and
  // the backend picks its own default profile.
  const profilesByMode = new Map();
  const currentMode = () => (imageInput?.files?.[0] ? "image_to_video" : "text_to_video");

  const isValidProfile = (profile) => Boolean(profile && profile.is_default && MODE_LABELS[profile.mode]
    && typeof profile.name === "string" && Array.isArray(profile.sizes) && Array.isArray(profile.frame_counts)
    && Number.isInteger(profile.default_frame_count));

  // [width, height] for "landscape" or "portrait", or null if the profile has no such size.
  const sizeFor = (profile, aspectRatio) => {
    if (!profile) {
      return FALLBACK_SIZES[aspectRatio] || null;
    }
    const portrait = aspectRatio === "portrait";
    const size = profile.sizes.find((entry) => Number.isInteger(entry?.width) && Number.isInteger(entry?.height)
      && (portrait ? entry.height > entry.width : entry.width >= entry.height));
    return size ? [size.width, size.height] : null;
  };

  const frameCountAllowed = (profile, frameCount) => !profile || profile.frame_counts.includes(Number(frameCount));

  // Disables the Settings options the current mode's profile does not support.
  // If the chosen option is one of them, the profile's default (or the first
  // supported option) is chosen instead.
  const applyProfileLimits = () => {
    const profile = profilesByMode.get(currentMode());
    const groups = {
      aspectRatio: (value) => Boolean(sizeFor(profile, value)),
      frameCount: (value) => frameCountAllowed(profile, value)
    };
    Object.entries(groups).forEach(([name, allowed]) => {
      const inputs = [...settingInputs].filter((input) => input.dataset.setting === name);
      inputs.forEach((input) => {
        input.disabled = !allowed(input.value);
      });
      const enabled = inputs.filter((input) => !input.disabled);
      if (enabled.length && !enabled.some((input) => input.value === generationSettings[name])) {
        const fallback = enabled.find((input) => name === "frameCount" && Number(input.value) === profile?.default_frame_count)
          || enabled[0];
        fallback.checked = true;
        generationSettings[name] = fallback.value;
      }
    });
  };

  const loadProfiles = async () => {
    try {
      const data = await apiRequest("/profiles/");
      (Array.isArray(data?.results) ? data.results : []).filter(isValidProfile)
        .forEach((profile) => profilesByMode.set(profile.mode, profile));
    } catch (error) {
      // Keep the built-in settings; generation still works with them.
    }
    applyProfileLimits();
  };

  const buildSettings = (mode) => {
    const profile = profilesByMode.get(mode);
    const [width, height] = sizeFor(profile, generationSettings.aspectRatio)
      || sizeFor(profile, "landscape") || sizeFor(profile, "portrait") || FALLBACK_SIZES.landscape;
    const frameCount = frameCountAllowed(profile, generationSettings.frameCount)
      ? Number(generationSettings.frameCount)
      : profile.default_frame_count;
    return {
      ...(profile ? { model_profile: profile.name } : {}),
      width,
      height,
      frame_count: frameCount,
      style: generationSettings.visualStyle
    };
  };

  const formatTime = (value) => (value ? new Date(value).toLocaleString() : "");

  // The API names the download (result.download_filename, the same name as the
  // video's Content-Disposition header); the fallback builds that same name.
  const downloadName = (job) => job.result?.download_filename
    || `geetha-${job.result?.is_mock ? "mock-" : ""}${job.job_id}.mp4`;

  // ---------------------------------------------------------------------------
  // Conversations. The backend has no conversation model, so conversations are
  // kept in this browser's localStorage: an ID, a title, timestamps, and the
  // turns (prompt text, image file name, job ID, last known status). Videos and
  // images are never stored; a job's result is always read from the API, which
  // enforces ownership. Conversations are not synchronised to other browsers.
  // ---------------------------------------------------------------------------
  const CONVERSATION_STORAGE_KEY = "geetha.conversations.v1";
  const MAX_CONVERSATIONS = 100;
  const MAX_TURNS = 200;
  const MAX_PROMPT_LENGTH = 1000;
  const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const TURN_STATUSES = new Set(["submitting", "queued", "processing", "completed", "failed", "error", "unavailable"]);
  const INTERRUPTED_MESSAGE = "The request was interrupted before it was sent. Please try again.";

  // Collision-resistant ID: randomUUID, or a version-4 UUID from getRandomValues
  // where randomUUID is unavailable (pages not served over HTTPS or localhost).
  const newId = () => {
    if (typeof window.crypto?.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    const bytes = window.crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  };

  const cleanText = (value, maxLength) => (typeof value === "string" ? value.slice(0, maxLength) : "");

  // Stored data is checked on every load; anything malformed is dropped.
  const cleanTurn = (turn) => {
    if (!turn || typeof turn.id !== "string" || !UUID_PATTERN.test(turn.id)) {
      return null;
    }
    const jobId = typeof turn.jobId === "string" && UUID_PATTERN.test(turn.jobId) ? turn.jobId : null;
    let status = TURN_STATUSES.has(turn.status) ? turn.status : "error";
    let error = cleanText(turn.error, 600);
    if (!jobId && status !== "error") {
      status = "error"; // the page was closed before the request was accepted
      error = INTERRUPTED_MESSAGE;
    }
    return {
      id: turn.id,
      prompt: cleanText(turn.prompt, MAX_PROMPT_LENGTH),
      imageName: cleanText(turn.imageName, 255),
      mode: turn.mode === "image_to_video" ? "image_to_video" : "text_to_video",
      jobId,
      status,
      error,
      createdAt: cleanText(turn.createdAt, 40)
    };
  };

  const cleanConversation = (conversation) => {
    if (!conversation || typeof conversation.id !== "string" || !UUID_PATTERN.test(conversation.id)) {
      return null;
    }
    const turns = (Array.isArray(conversation.turns) ? conversation.turns : []).map(cleanTurn).filter(Boolean).slice(-MAX_TURNS);
    if (!turns.length) {
      return null; // an empty chat is never stored
    }
    return {
      id: conversation.id,
      title: cleanText(conversation.title, 120) || "Untitled chat",
      pinned: conversation.pinned === true,
      createdAt: cleanText(conversation.createdAt, 40),
      updatedAt: cleanText(conversation.updatedAt, 40),
      turns
    };
  };

  const readConversations = () => {
    try {
      const stored = JSON.parse(window.localStorage.getItem(CONVERSATION_STORAGE_KEY) || "null");
      const seen = new Set();
      const list = (Array.isArray(stored?.conversations) ? stored.conversations : [])
        .map(cleanConversation)
        .filter((conversation) => conversation && !seen.has(conversation.id) && seen.add(conversation.id));
      return { list, activeId: list.some((conversation) => conversation.id === stored?.activeId) ? stored.activeId : null };
    } catch (error) {
      return { list: [], activeId: null }; // storage unavailable or corrupted
    }
  };

  const storedConversations = readConversations();
  let conversations = storedConversations.list;
  // The open chat. A new chat gets its ID immediately and is saved with its first prompt.
  let activeConversationId = storedConversations.activeId || newId();

  const saveConversations = () => {
    // Newest first; beyond the limit the oldest unpinned conversations are dropped.
    conversations.sort((a, b) => (b.updatedAt || "").localeCompare(a.updatedAt || ""));
    while (conversations.length > MAX_CONVERSATIONS) {
      const oldestUnpinned = conversations.map((conversation) => conversation.pinned).lastIndexOf(false);
      conversations.splice(oldestUnpinned === -1 ? conversations.length - 1 : oldestUnpinned, 1);
    }
    try {
      window.localStorage.setItem(CONVERSATION_STORAGE_KEY, JSON.stringify({
        version: 1,
        activeId: conversations.some((conversation) => conversation.id === activeConversationId) ? activeConversationId : null,
        conversations
      }));
    } catch (error) {
      // Storage full or blocked: conversations still work for this page view.
    }
  };

  const activeConversation = () => conversations.find((conversation) => conversation.id === activeConversationId) || null;

  const findTurnByJob = (jobId) => {
    for (const conversation of conversations) {
      const turn = conversation.turns.find((candidate) => candidate.jobId === jobId);
      if (turn) {
        return { conversation, turn };
      }
    }
    return null;
  };

  // ---------------------------------------------------------------------------
  // Composer lock. One video generates at a time per browser session, so Send
  // waits while any of the session's jobs is queued or processing. In the chat
  // whose video is generating, the prompt and image are also read-only; in any
  // other chat (for example a new one) the user can type straight away. The lock
  // follows the job status reported by the API, which only returns the session's
  // own jobs, so other users are never blocked.
  // ---------------------------------------------------------------------------
  const ACTIVE_STATUSES = new Set(["queued", "processing"]);
  const activeJobIds = new Set();
  let submitting = false;
  const inputControls = [prompt, imageInput, composerPlus, removeImage].filter(Boolean);

  const isComposerLocked = () => submitting
    || Boolean(activeConversation()?.turns.some((turn) => turn.jobId && activeJobIds.has(turn.jobId)));
  const isSubmitBlocked = () => submitting || activeJobIds.size > 0;

  const updateComposerLock = () => {
    const locked = isComposerLocked();
    inputControls.forEach((control) => {
      control.disabled = locked;
    });
    if (submitButton) {
      submitButton.disabled = isSubmitBlocked();
    }
    composer?.classList.toggle("is-locked", locked);
    composerForm?.setAttribute("aria-busy", String(locked));
  };

  // Records a job status from the API. Returns true when a job this page was
  // waiting for has just finished.
  const trackJobStatus = (jobId, status) => {
    if (ACTIVE_STATUSES.has(status)) {
      activeJobIds.add(jobId);
      updateComposerLock();
      return false;
    }
    const wasActive = activeJobIds.delete(jobId);
    updateComposerLock();
    return wasActive;
  };

  const showFinishedFeedback = (job) => {
    if (!formMessage) {
      return;
    }
    const owner = findTurnByJob(job.job_id)?.conversation;
    if (job.status === "completed") {
      formMessage.textContent = owner && owner.id !== activeConversationId
        ? `${job.result?.is_mock ? "Mock video" : "Video"} ready in “${owner.title}”. Open it from Recent Chats.`
        : `${job.result?.is_mock ? "Mock video" : "Video"} ready. Open it from Recent Chats or the Library.`;
      formMessage.classList.remove("is-error");
    } else if (job.error?.code === "cancelled") {
      formMessage.textContent = "Video cancelled. You can send a new prompt.";
      formMessage.classList.remove("is-error");
    } else {
      formMessage.textContent = job.error?.message || "The video could not be generated.";
      formMessage.classList.add("is-error");
    }
  };

  // ---------------------------------------------------------------------------
  // Chat messages. A submitted prompt is shown as a user message with a
  // "Generating video" card beneath it; the job's status polling replaces the
  // card with the video player when the job completes (or with the error if it
  // fails). The API reports no numeric progress, so the bar is indeterminate.
  // Only the open conversation is drawn; every turn is stored in its own
  // conversation, so results never appear in another chat.
  // ---------------------------------------------------------------------------
  const chatScroll = document.querySelector("[data-chat-scroll]");
  const chatMessages = document.querySelector("[data-chat-messages]");
  const emptyState = document.querySelector("[data-empty-state]");
  const turnElements = new Map(); // turn ID -> its assistant message, open conversation only
  const FINISHED_STATUSES = new Set(["completed", "failed"]);

  const createElement = (tag, className, text) => {
    const element = document.createElement(tag);
    element.className = className;
    if (text !== undefined) {
      element.textContent = text; // never HTML: prompts are shown exactly as typed
    }
    return element;
  };

  const scrollChatToEnd = () => {
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    chatScroll?.scrollTo({ top: chatScroll.scrollHeight, behavior: reduceMotion ? "auto" : "smooth" });
  };

  const addUserMessage = (text, imageName) => {
    const message = createElement("div", "chat-message chat-message-user");
    const bubble = createElement("div", "chat-bubble");
    if (text) {
      bubble.append(createElement("p", "chat-text", text));
    }
    if (imageName) {
      bubble.append(createElement("p", "chat-attachment", `▧ ${imageName}`));
    }
    message.append(bubble);
    chatMessages?.append(message);
    if (chatMessages) {
      chatMessages.hidden = false;
    }
    if (emptyState) {
      emptyState.hidden = true;
    }
  };

  // A spinner card. The "Generating video" card also has the indeterminate bar.
  const spinnerCard = (title, text, withBar) => {
    const card = createElement("div", "generation-card");
    card.setAttribute("role", "status");
    const head = createElement("div", "generation-head");
    const spinner = createElement("span", "generation-spinner");
    spinner.setAttribute("aria-hidden", "true");
    const copy = createElement("div", "generation-copy");
    copy.append(createElement("p", "generation-title", title), createElement("p", "generation-text", text));
    head.append(spinner, copy);
    card.append(head);
    if (withBar) {
      // No aria-valuenow: an indeterminate progress bar, never a made-up percentage.
      const bar = createElement("div", "generation-bar");
      bar.setAttribute("role", "progressbar");
      bar.setAttribute("aria-label", "Generating video");
      bar.append(createElement("span", "generation-bar-fill"));
      card.append(bar);
    }
    return card;
  };

  const addAssistantMessage = () => {
    const message = createElement("div", "chat-message chat-message-assistant");
    chatMessages?.append(message);
    return message;
  };

  const showGenerationFailure = (message, text, title = "Video generation failed") => {
    const card = createElement("div", "generation-card is-failed");
    card.setAttribute("role", "status");
    card.append(
      createElement("p", "generation-title", title),
      createElement("p", "generation-text", text)
    );
    message.replaceChildren(card);
    scrollChatToEnd();
  };

  // Replaces a job's loading card once the job has finished.
  const showGenerationResult = (message, job) => {
    if (job.error?.code === "cancelled") {
      showGenerationFailure(message, job.error.message, "Video cancelled");
      return;
    }
    if (job.status !== "completed" || !job.result?.preview_url) {
      const reference = job.error?.request_id ? ` (ref ${job.error.request_id})` : "";
      showGenerationFailure(message, `${job.error?.message || "The video could not be generated."}${reference}`);
      return;
    }
    const isMock = Boolean(job.result.is_mock);
    const figure = createElement("figure", "generation-result");
    const video = createElement("video", "generation-video");
    video.controls = true;
    video.playsInline = true;
    video.preload = "metadata";
    video.src = job.result.preview_url;
    video.setAttribute("aria-label", `Generated video: ${job.prompt || MODE_LABELS[job.mode] || "Untitled"}`);
    const caption = createElement("figcaption", "generation-caption");
    const label = createElement("span", `generation-label${isMock ? " is-mock" : ""}`,
      isMock ? "Mock test video · not AI-generated" : "Video ready");
    caption.append(label);
    if (job.result.download_url) {
      const download = createElement("a", "generation-download", "↓ Download");
      download.href = job.result.download_url;
      download.download = downloadName(job);
      caption.append(download);
    }
    video.addEventListener("error", () => {
      label.textContent = "The video could not be played. Try the download link.";
      label.classList.add("is-error");
    });
    figure.append(video, caption);
    message.replaceChildren(figure);
    scrollChatToEnd();
  };

  // Cancels a queued job. On success the job is finished like any other (its own
  // conversation shows "Video cancelled" and Send is enabled again); if a worker
  // started it first, the API refuses and polling keeps showing its progress.
  const cancelJob = async (jobId, button) => {
    button.disabled = true;
    try {
      const job = await apiRequest(`/generations/${jobId}/cancel/`, { method: "POST" });
      clearInterval(pollers.get(jobId));
      pollers.delete(jobId);
      updateGenerationMessage(jobId, job);
      if (trackJobStatus(jobId, job.status)) {
        showFinishedFeedback(job);
      }
      recordFinishedJob(job);
    } catch (error) {
      if (formMessage) {
        formMessage.textContent = error.message;
        formMessage.classList.toggle("is-error", error.code !== "not_cancellable");
      }
      if (error.code === "not_cancellable" || error.status === 404) {
        button.remove(); // already started or gone; polling updates the card
      } else {
        button.disabled = false; // for example a network error: the user may try again
      }
    }
  };

  // Shows the Cancel button on a loading card only while its job is queued.
  const syncCancelButton = (turn, message) => {
    const card = message.querySelector(".generation-card");
    const existing = card?.querySelector(".generation-cancel");
    if (!card || turn.status !== "queued" || !turn.jobId) {
      existing?.remove();
      return;
    }
    if (!existing) {
      const button = createElement("button", "generation-download generation-cancel", "Cancel");
      button.type = "button";
      button.setAttribute("aria-label", "Cancel this video while it is waiting to start");
      button.addEventListener("click", () => cancelJob(turn.jobId, button));
      card.append(button);
    }
  };

  // Draws one turn's assistant message from its stored state. A finished turn's
  // result is read from the API (`job` when the caller already has it).
  const paintTurn = (turn, message, job = null) => {
    if (turn.status === "error") {
      showGenerationFailure(message, turn.error || "The video could not be generated.");
      return;
    }
    if (turn.status === "unavailable") {
      showGenerationFailure(message, "This video is no longer available.");
      return;
    }
    if (!FINISHED_STATUSES.has(turn.status)) {
      if (!message.querySelector(".generation-bar")) {
        message.replaceChildren(spinnerCard("Generating video", "Creating your scene…", true));
      }
      syncCancelButton(turn, message);
      return;
    }
    if (job) {
      showGenerationResult(message, job);
      return;
    }
    message.replaceChildren(spinnerCard("Loading video", "Fetching the saved result…", false));
    apiRequest(`/generations/${turn.jobId}/`)
      .then((saved) => {
        if (message.isConnected) {
          showGenerationResult(message, saved);
        }
      })
      .catch((error) => {
        if (error.status === 404) {
          updateGenerationMessage(turn.jobId, null);
        } else if (message.isConnected) {
          showGenerationFailure(message, `The saved result could not be loaded. ${error.message}`);
        }
      });
  };

  // Called with every status the poller receives for a job (null: the job is
  // gone). The status is saved to the job's own conversation; the screen is
  // updated only if that conversation is the one being shown.
  const updateGenerationMessage = (jobId, job) => {
    const found = findTurnByJob(jobId);
    if (!found) {
      return; // a generation from before conversations existed
    }
    const { conversation, turn } = found;
    const status = job ? job.status : "unavailable";
    if (turn.status === status) {
      return;
    }
    turn.status = status;
    saveConversations();
    renderSidebarLists();
    const message = conversation.id === activeConversationId ? turnElements.get(turn.id) : null;
    if (message?.isConnected) {
      paintTurn(turn, message, job);
    }
  };

  // Shows the open conversation, or the empty state for a new chat.
  const renderActiveConversation = () => {
    turnElements.clear();
    chatMessages?.replaceChildren();
    const turns = activeConversation()?.turns || [];
    turns.forEach((turn) => {
      addUserMessage(turn.prompt, turn.imageName);
      const message = addAssistantMessage();
      turnElements.set(turn.id, message);
      paintTurn(turn, message);
    });
    if (chatMessages) {
      chatMessages.hidden = turns.length === 0;
    }
    if (emptyState) {
      emptyState.hidden = turns.length > 0;
    }
    if (chatScroll) {
      chatScroll.scrollTop = chatScroll.scrollHeight;
    }
  };

  const pollJob = (jobId) => {
    if (pollers.has(jobId)) {
      return;
    }
    const tick = async () => {
      try {
        const job = await apiRequest(`/generations/${jobId}/`);
        if (jobId === previewJobId) {
          renderPreview(job);
        }
        updateGenerationMessage(jobId, job);
        if (trackJobStatus(jobId, job.status)) {
          showFinishedFeedback(job);
        }
        if (job.status === "completed" || job.status === "failed") {
          clearInterval(pollers.get(jobId));
          pollers.delete(jobId);
          recordFinishedJob(job);
        }
      } catch (error) {
        // 404: the job is not (or no longer) this session's, so it cannot hold
        // the lock. Other errors keep the current state and polling retries.
        if (error.status === 404) {
          clearInterval(pollers.get(jobId));
          pollers.delete(jobId);
          trackJobStatus(jobId, "unknown");
          updateGenerationMessage(jobId, null);
          return;
        }
        // Keep polling; a transient network error should not lose the job.
        if (jobId === previewJobId && !previewVideoUrl) {
          setPreviewState({
            status: "Connection problem",
            tone: "failed",
            icon: "!",
            heading: "Unable to load this video",
            text: error.message
          });
        }
      }
    };
    pollers.set(jobId, setInterval(tick, POLL_INTERVAL_MS));
    tick();
  };

  const historyButton = (item) => {
    const button = document.createElement("button");
    button.className = "recent-item";
    button.type = "button";
    const icon = document.createElement("span");
    icon.className = "play-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = item.video_available ? "▶" : "◷";
    const copy = document.createElement("span");
    copy.className = "recent-copy";
    const title = document.createElement("span");
    title.className = "recent-title";
    title.textContent = item.prompt_preview || MODE_LABELS[item.mode];
    const meta = document.createElement("span");
    meta.className = "recent-meta";
    meta.textContent = `${item.status} · ${formatTime(item.created_at)}`;
    copy.append(title, meta);
    button.append(icon, copy);
    button.addEventListener("click", () => {
      closeSidebar();
      selectPreview(item.job_id, null, { focus: true });
    });
    return button;
  };

  // ---------------------------------------------------------------------------
  // Pinned Chats. The backend has no chat model: each sidebar "chat" is a
  // generation job. Pins are kept in this browser only, as job IDs (no prompt
  // text or images), and pinned jobs are always loaded through the API, which
  // enforces ownership. A pin whose job returns 404 is dropped.
  // ---------------------------------------------------------------------------
  const PIN_STORAGE_KEY = "geetha.pinnedJobIds";
  const MAX_PINS = 50;
  const JOB_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  let recentItems = [];
  const pinnedOutsideRecent = new Map();

  const readPins = () => {
    try {
      const stored = JSON.parse(window.localStorage.getItem(PIN_STORAGE_KEY) || "[]");
      return Array.isArray(stored)
        ? [...new Set(stored.filter((id) => typeof id === "string" && JOB_ID_PATTERN.test(id)))].slice(0, MAX_PINS)
        : [];
    } catch (error) {
      return []; // Storage unavailable or corrupted: start with no pins.
    }
  };
  let pinnedIds = readPins();

  const savePins = () => {
    try {
      window.localStorage.setItem(PIN_STORAGE_KEY, JSON.stringify(pinnedIds));
    } catch (error) {
      // Pins still work for this page view if storage is blocked.
    }
  };

  // Converts a job detail response into the list-item shape the sidebar uses.
  const toListItem = (job) => {
    const text = job.prompt || "";
    return {
      job_id: job.job_id,
      mode: job.mode,
      status: job.status,
      prompt_preview: text.length <= 80 ? text : `${text.slice(0, 77)}...`,
      created_at: job.created_at,
      completed_at: job.completed_at,
      video_available: Boolean(job.result?.preview_url)
    };
  };

  const pinIcon = () => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", "14");
    svg.setAttribute("height", "14");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M9 3h6l-1 6 3 3v2H7v-2l3-3-1-6zM12 14v7");
    svg.append(path);
    return svg;
  };

  // Remember where a row was: only one sidebar panel is open at a time, so the
  // moved row usually ends up in a closed panel.
  const pinSource = (id, wasPinned) => {
    const sourceList = wasPinned ? pinnedList : historyList;
    const sourceButtons = [...(sourceList?.querySelectorAll("[data-pin-for]") || [])];
    return { sourceList, sourceIndex: sourceButtons.findIndex((button) => button.dataset.pinFor === id) };
  };

  const togglePin = (item) => {
    const wasPinned = pinnedIds.includes(item.job_id);
    const source = pinSource(item.job_id, wasPinned);
    pinnedIds = wasPinned
      ? pinnedIds.filter((id) => id !== item.job_id)
      : [item.job_id, ...pinnedIds].slice(0, MAX_PINS);
    if (wasPinned) {
      pinnedOutsideRecent.delete(item.job_id);
    } else if (!recentItems.some((recent) => recent.job_id === item.job_id)) {
      pinnedOutsideRecent.set(item.job_id, item);
    }
    savePins();
    renderSidebarLists();
    afterPinChange(item.job_id, wasPinned, source, item.prompt_preview || MODE_LABELS[item.mode] || "Untitled");
  };

  // Conversations are pinned with a flag stored in the conversation itself.
  const togglePinConversation = (conversation) => {
    const wasPinned = conversation.pinned;
    const source = pinSource(conversation.id, wasPinned);
    conversation.pinned = !wasPinned;
    saveConversations();
    renderSidebarLists();
    afterPinChange(conversation.id, wasPinned, source, conversation.title);
  };

  // Keep keyboard focus in the list the user is working in, and announce
  // where the chat went for screen-reader users.
  const afterPinChange = (id, wasPinned, { sourceList, sourceIndex }, label) => {
    if (pinStatus) {
      pinStatus.textContent = wasPinned
        ? `Unpinned: ${label}. It is back in Recent Chats.`
        : `Pinned: ${label}. Find it under Pinned Chats.`;
    }
    const moved = document.querySelector(`[data-pin-for="${id}"]`);
    if (moved && moved.offsetParent !== null) {
      moved.focus();
      return;
    }
    const remaining = [...(sourceList?.querySelectorAll("[data-pin-for]") || [])];
    const next = remaining[Math.min(sourceIndex, remaining.length - 1)];
    if (next) {
      next.focus();
    } else {
      document.querySelector(`[aria-controls="${sourceList?.closest(".sidebar-panel")?.id}"]`)?.focus();
    }
  };

  // A sidebar chat row: the existing chat button plus a pin/unpin toggle.
  const historyRow = (item, isPinned) => {
    const row = document.createElement("div");
    row.className = "recent-row";
    const label = item.prompt_preview || MODE_LABELS[item.mode] || "Untitled";
    const pinButton = document.createElement("button");
    pinButton.type = "button";
    pinButton.className = `pin-button${isPinned ? " is-pinned" : ""}`;
    pinButton.dataset.pinFor = item.job_id;
    pinButton.setAttribute("aria-pressed", String(isPinned));
    pinButton.setAttribute("aria-label", `${isPinned ? "Unpin" : "Pin"} chat: ${label}`);
    pinButton.title = isPinned ? "Unpin" : "Pin";
    pinButton.append(pinIcon());
    pinButton.addEventListener("click", () => togglePin(item));
    row.append(historyButton(item), pinButton);
    return row;
  };

  // A sidebar conversation row: opens the conversation; plus a pin toggle.
  const conversationRow = (conversation, isPinned) => {
    const row = createElement("div", "recent-row");
    const isCurrent = conversation.id === activeConversationId;
    const button = createElement("button", `recent-item${isCurrent ? " is-current" : ""}`);
    button.type = "button";
    if (isCurrent) {
      button.setAttribute("aria-current", "true");
    }
    const generating = conversation.turns.some((turn) => ["submitting", "queued", "processing"].includes(turn.status));
    const icon = createElement("span", "play-icon", generating ? "◷" : "▤");
    icon.setAttribute("aria-hidden", "true");
    const copy = createElement("span", "recent-copy");
    const count = conversation.turns.length;
    copy.append(
      createElement("span", "recent-title", conversation.title),
      createElement("span", "recent-meta", `${generating ? "generating" : `${count} prompt${count === 1 ? "" : "s"}`} · ${formatTime(conversation.updatedAt)}`)
    );
    button.append(icon, copy);
    button.addEventListener("click", () => openConversation(conversation.id));
    const pinButton = createElement("button", `pin-button${isPinned ? " is-pinned" : ""}`);
    pinButton.type = "button";
    pinButton.dataset.pinFor = conversation.id;
    pinButton.setAttribute("aria-pressed", String(isPinned));
    pinButton.setAttribute("aria-label", `${isPinned ? "Unpin" : "Pin"} chat: ${conversation.title}`);
    pinButton.title = isPinned ? "Unpin" : "Pin";
    pinButton.append(pinIcon());
    pinButton.addEventListener("click", () => togglePinConversation(conversation));
    row.append(button, pinButton);
    return row;
  };

  // Recent Chats lists this browser's conversations. Generations that belong
  // to none of them (made before conversations existed) stay listed below as
  // "Earlier generations", opening in the Video Preview as before.
  const renderSidebarLists = () => {
    const recentById = new Map(recentItems.map((item) => [item.job_id, item]));
    const pinnedJobs = pinnedIds
      .map((id) => recentById.get(id) || pinnedOutsideRecent.get(id))
      .filter(Boolean);
    const pinnedSet = new Set(pinnedIds);
    const conversationJobs = new Set(conversations.flatMap((conversation) => conversation.turns.map((turn) => turn.jobId)));
    const earlier = recentItems.filter((item) => !pinnedSet.has(item.job_id) && !conversationJobs.has(item.job_id));
    const pinnedConversations = conversations.filter((conversation) => conversation.pinned);

    pinnedList?.replaceChildren(
      ...pinnedConversations.map((conversation) => conversationRow(conversation, true)),
      ...pinnedJobs.map((item) => historyRow(item, true))
    );
    if (pinnedEmpty) {
      pinnedEmpty.hidden = pinnedConversations.length + pinnedJobs.length > 0;
    }
    const rows = conversations.filter((conversation) => !conversation.pinned).map((conversation) => conversationRow(conversation, false));
    if (earlier.length) {
      rows.push(createElement("h3", "recent-group-heading", "Earlier generations"), ...earlier.map((item) => historyRow(item, false)));
    }
    historyList?.replaceChildren(...rows);
    if (historyEmpty) {
      historyEmpty.hidden = rows.length > 0;
      historyEmpty.textContent = conversations.length || recentItems.length
        ? "All of your recent chats are pinned."
        : "Your chats appear here.";
    }
    const finished = recentItems.filter((item) => item.video_available);
    libraryList?.replaceChildren(...finished.map(historyButton));
    if (libraryEmpty) {
      libraryEmpty.hidden = finished.length > 0;
    }
  };

  // Brings a finished job's sidebar entry up to date. A job the sidebar already
  // lists is updated in place, so previewing a finished video sends no history
  // request; history is reloaded only for a job it does not list yet.
  const recordFinishedJob = (job) => {
    const item = toListItem(job);
    const index = recentItems.findIndex((entry) => entry.job_id === job.job_id);
    const known = index >= 0 ? recentItems[index] : pinnedOutsideRecent.get(job.job_id);
    if (!known) {
      loadHistory();
      return;
    }
    if (known.status === item.status && known.video_available === item.video_available) {
      return;
    }
    if (index >= 0) {
      recentItems[index] = { ...known, ...item };
    } else {
      pinnedOutsideRecent.set(job.job_id, { ...known, ...item });
    }
    renderSidebarLists();
  };

  // Loads pinned jobs that are older than the recent list.
  const loadPinnedOutsideRecent = async () => {
    const recentIds = new Set(recentItems.map((item) => item.job_id));
    const missing = pinnedIds.filter((id) => !recentIds.has(id));
    if (!missing.length) {
      return;
    }
    await Promise.all(missing.map(async (id) => {
      try {
        pinnedOutsideRecent.set(id, toListItem(await apiRequest(`/generations/${id}/`)));
      } catch (error) {
        if (error.status === 404) {
          pinnedIds = pinnedIds.filter((pinnedId) => pinnedId !== id);
          pinnedOutsideRecent.delete(id);
          savePins();
        }
      }
    }));
    renderSidebarLists();
  };

  // Loads recent jobs for the Pinned Chats, Recent Chats, and Library lists.
  let historyLoading = false;
  let historyReloadRequested = false;
  const loadHistory = async () => {
    if (historyLoading) {
      historyReloadRequested = true; // Run again once the current load finishes.
      return;
    }
    historyLoading = true;
    try {
      const page = await apiRequest("/generations/?page_size=30");
      const items = (Array.isArray(page?.results) ? page.results : []).filter((item) => item && item.job_id);
      recentItems = items;
      pinnedOutsideRecent.clear();
      pinnedIds = readPins(); // Pick up pins changed in another tab.
      renderSidebarLists();
      loadPinnedOutsideRecent();
      // Resume tracking of jobs that were still running when the page loaded,
      // and lock the composer while any of them is active.
      // A tracked job that history shows as finished is polled once more, so the
      // full job (mock flag, error message) drives the feedback and the unlock.
      items
        .filter((item) => ACTIVE_STATUSES.has(item.status) || activeJobIds.has(item.job_id))
        .forEach((item) => {
          if (ACTIVE_STATUSES.has(item.status)) {
            trackJobStatus(item.job_id, item.status);
          }
          pollJob(item.job_id);
        });
    } catch (error) {
      if (historyEmpty) {
        historyEmpty.hidden = false;
        historyEmpty.textContent = error.message;
      }
    } finally {
      historyLoading = false;
      if (historyReloadRequested) {
        historyReloadRequested = false;
        loadHistory();
      }
    }
  };

  const submitGeneration = async (promptText) => {
    const file = imageInput?.files?.[0];
    const settings = buildSettings(file ? "image_to_video" : "text_to_video");
    let path;
    let options;
    if (file) {
      const formData = new FormData();
      formData.append("image", file);
      formData.append("prompt", promptText);
      formData.append("settings", JSON.stringify(settings));
      path = "/generations/image-to-video/";
      options = { method: "POST", body: formData };
    } else {
      path = "/generations/text-to-video/";
      options = {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: promptText, settings })
      };
    }
    return apiRequest(path, options);
  };

  // ---------------------------------------------------------------------------
  // Video Preview panel. A non-modal panel with one native <video> player. It
  // only receives URLs that the API returned for a completed job; nothing is
  // invented, and there is no autoplay.
  // ---------------------------------------------------------------------------
  let previewJobId = null;
  let previewVideoUrl = null;
  let previewDownloadUrl = null;
  let previewDownloadName = "geetha-video.mp4";
  let previewIsMock = false;

  const EMPTY_PREVIEW = {
    status: "No video",
    tone: "empty",
    icon: "▶",
    heading: "No video to preview yet",
    text: "Your generated video will appear here when a generation finishes."
  };

  const isPreviewOpen = () => Boolean(previewPanel && !previewPanel.hidden);

  const setPreviewStatus = (label, tone) => {
    if (previewStatus) {
      previewStatus.textContent = label;
      previewStatus.className = `preview-status is-${tone}`;
    }
  };

  const hideVideoAndDownload = () => {
    if (previewVideo) {
      previewVideo.pause();
      previewVideo.hidden = true;
    }
    if (previewActions) {
      previewActions.hidden = true;
    }
    if (previewDownload) {
      previewDownload.removeAttribute("href");
    }
  };

  // Shows a message instead of the player (empty, waiting, or error states).
  const setPreviewState = ({ status, tone, icon, heading, text, retry = false, busy = false }) => {
    hideVideoAndDownload();
    setPreviewStatus(status, tone);
    if (previewState) {
      previewState.hidden = false;
      previewState.classList.toggle("is-busy", busy);
    }
    if (previewStateIcon) {
      previewStateIcon.textContent = icon;
    }
    if (previewStateHeading) {
      previewStateHeading.textContent = heading;
    }
    if (previewStateText) {
      previewStateText.textContent = text;
    }
    if (previewRetry) {
      previewRetry.hidden = !retry;
    }
  };

  const unloadPreviewVideo = () => {
    previewVideoUrl = null;
    previewDownloadUrl = null;
    previewIsMock = false;
    if (previewVideo) {
      previewVideo.pause();
      previewVideo.removeAttribute("src");
      previewVideo.load();
    }
  };

  const setPreviewCaption = (job) => {
    if (!previewCaption) {
      return;
    }
    if (!job) {
      previewCaption.hidden = true;
      previewCaption.textContent = "";
      return;
    }
    const promptText = job.prompt || (job.mode === "image_to_video" ? "Animate the uploaded image" : "Untitled");
    const size = job.settings ? ` · ${job.settings.width}×${job.settings.height} · ${job.settings.frame_count} frames` : "";
    previewCaption.textContent = `${promptText} — ${MODE_LABELS[job.mode] || job.mode}${size}`;
    previewCaption.hidden = false;
  };

  const resetPreview = () => {
    previewJobId = null;
    unloadPreviewVideo();
    setPreviewCaption(null);
    if (previewMock) {
      previewMock.hidden = true;
    }
    setPreviewState(EMPTY_PREVIEW);
  };

  // Updates the panel from a job object returned by GET /generations/{id}/.
  const renderPreview = (job) => {
    setPreviewCaption(job);
    const isMock = Boolean(job.result?.is_mock);
    if (previewMock) {
      previewMock.hidden = !isMock;
    }

    if (job.status === "queued" || job.status === "processing") {
      unloadPreviewVideo();
      setPreviewState({
        status: job.status === "queued" ? "Queued" : "Generating",
        tone: "busy",
        icon: "◷",
        heading: job.status === "queued" ? "Waiting in the queue" : "Generating your video",
        text: "The preview will load automatically when the video is ready.",
        busy: true
      });
      return;
    }

    // Cancellation is stored as `failed` + code `cancelled`; the chat card says
    // "Video cancelled", so the preview must not call it a failure.
    if (job.status === "failed" && job.error?.code === "cancelled") {
      unloadPreviewVideo();
      setPreviewState({
        status: "Cancelled",
        tone: "empty",
        icon: "×",
        heading: "Video cancelled",
        text: job.error.message || "You cancelled this video."
      });
      return;
    }

    if (job.status === "failed") {
      unloadPreviewVideo();
      const reference = job.error?.request_id ? ` (ref ${job.error.request_id})` : "";
      setPreviewState({
        status: "Failed",
        tone: "failed",
        icon: "!",
        heading: "This generation failed",
        text: `${job.error?.message || "The video could not be generated."}${reference}`
      });
      return;
    }

    const videoUrl = job.status === "completed" ? job.result?.preview_url : null;
    if (!videoUrl) {
      unloadPreviewVideo();
      setPreviewState(EMPTY_PREVIEW);
      return;
    }

    // Download is offered only when the API supplied a download URL for this job,
    // and only once the video has actually loaded (see showLoadedVideo).
    previewDownloadUrl = job.result.download_url || null;
    previewIsMock = isMock;
    previewDownloadName = downloadName(job);

    if (videoUrl === previewVideoUrl) {
      return; // Already loaded; avoid restarting playback on repeated polls.
    }
    previewVideoUrl = videoUrl;
    setPreviewState({
      status: "Loading",
      tone: "busy",
      icon: "◷",
      heading: "Loading video…",
      text: "Fetching the video from the server.",
      busy: true
    });
    if (previewVideo) {
      previewVideo.src = videoUrl;
      previewVideo.load();
    }
  };

  const showLoadedVideo = () => {
    if (!previewVideoUrl || !previewVideo) {
      return;
    }
    if (previewState) {
      previewState.hidden = true;
    }
    previewVideo.hidden = false;
    setPreviewStatus(previewIsMock ? "Mock video ready" : "Video ready", "ready");
    if (previewActions && previewDownload && previewDownloadUrl) {
      previewDownload.href = previewDownloadUrl;
      previewDownload.setAttribute("download", previewDownloadName);
      previewActions.hidden = false;
    }
  };

  const showVideoLoadError = () => {
    if (!previewVideoUrl) {
      return; // Errors from clearing the source are expected.
    }
    setPreviewState({
      status: "Error",
      tone: "failed",
      icon: "!",
      heading: "Unable to load this video",
      text: "The file could not be played. It may have been removed, or the connection was interrupted.",
      retry: true
    });
  };

  const openPreview = ({ focus = false } = {}) => {
    if (!previewPanel) {
      return;
    }
    previewPanel.hidden = false;
    previewToggle?.setAttribute("aria-expanded", "true");
    previewToggle?.setAttribute("aria-label", "Hide video preview");
    previewToggle?.classList.add("is-active");
    if (focus) {
      previewHeading?.focus({ preventScroll: true });
    }
    // The panel lives in the scrolling conversation area, so bring it into view.
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    previewPanel.scrollIntoView({ block: "nearest", behavior: reduceMotion ? "auto" : "smooth" });
  };

  const closePreview = ({ returnFocus = false } = {}) => {
    if (!previewPanel) {
      return;
    }
    previewVideo?.pause();
    previewPanel.hidden = true;
    previewToggle?.setAttribute("aria-expanded", "false");
    previewToggle?.setAttribute("aria-label", "Show video preview");
    previewToggle?.classList.remove("is-active");
    if (returnFocus) {
      previewToggle?.focus();
    }
  };

  // Points the preview at a job and keeps it updated until it finishes.
  const selectPreview = (jobId, job, options = {}) => {
    if (jobId !== previewJobId) {
      unloadPreviewVideo();
    }
    previewJobId = jobId;
    if (job) {
      renderPreview(job);
    } else if (!previewVideoUrl) {
      // Selecting the job whose video is already loaded keeps showing it; the
      // poll below refreshes its status without reloading the video.
      setPreviewCaption(null);
      setPreviewState({
        status: "Loading",
        tone: "busy",
        icon: "◷",
        heading: "Loading video…",
        text: "Checking this generation's status.",
        busy: true
      });
    }
    openPreview(options);
    pollJob(jobId);
  };

  if (previewToggle) {
    previewToggle.addEventListener("click", () => {
      if (isPreviewOpen()) {
        closePreview();
      } else {
        openPreview();
      }
    });
  }

  if (previewClose) {
    previewClose.addEventListener("click", () => closePreview({ returnFocus: true }));
  }

  if (previewPanel) {
    // Non-modal panel: Escape closes it only while focus is inside it.
    previewPanel.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        closePreview({ returnFocus: true });
      }
    });
  }

  if (previewRetry) {
    previewRetry.addEventListener("click", () => {
      const url = previewVideoUrl;
      if (url && previewVideo) {
        setPreviewState({
          status: "Loading",
          tone: "busy",
          icon: "◷",
          heading: "Loading video…",
          text: "Trying again.",
          busy: true
        });
        previewVideo.src = url;
        previewVideo.load();
      }
    });
  }

  if (previewVideo) {
    previewVideo.addEventListener("loadeddata", showLoadedVideo);
    previewVideo.addEventListener("error", showVideoLoadError);
  }

  resetPreview();

  const closeSidebar = () => {
    body.classList.remove("sidebar-open");
    if (sidebarToggle) {
      sidebarToggle.setAttribute("aria-expanded", "false");
      sidebarToggle.setAttribute("aria-label", "Open navigation menu");
    }
  };

  const openSidebar = () => {
    body.classList.add("sidebar-open");
    if (sidebarToggle) {
      sidebarToggle.setAttribute("aria-expanded", "true");
      sidebarToggle.setAttribute("aria-label", "Close navigation menu");
    }
  };

  if (sidebarToggle) {
    sidebarToggle.addEventListener("click", () => {
      const isOpen = body.classList.contains("sidebar-open");
      if (isOpen) {
        closeSidebar();
      } else {
        openSidebar();
      }
    });
  }

  if (sidebarClose) {
    sidebarClose.addEventListener("click", closeSidebar);
  }

  if (sidebarBackdrop) {
    sidebarBackdrop.addEventListener("click", closeSidebar);
  }

  // Empties the composer when another chat is shown. Nothing is sent or deleted.
  const resetComposer = () => {
    if (prompt) {
      prompt.value = "";
      prompt.style.height = "auto";
    }
    if (imagePreview && imagePreview.hidden === false) {
      clearImagePreview();
    }
    if (formMessage) {
      formMessage.textContent = "";
      formMessage.classList.remove("is-error");
    }
  };

  // Tells the user why Send is waiting when another chat's video is generating.
  const noteOtherChatGenerating = () => {
    if (formMessage && isSubmitBlocked() && !isComposerLocked()) {
      formMessage.textContent = "A video is still generating in another chat. You can type now and send when it finishes.";
      formMessage.classList.remove("is-error");
    }
  };

  const showConversation = (id) => {
    activeConversationId = id;
    saveConversations();
    resetComposer();
    renderActiveConversation();
    updateComposerLock();
    renderSidebarLists();
    noteOtherChatGenerating();
  };

  // New Chat: a new, empty conversation with its own ID. Earlier conversations,
  // their running jobs and their videos are untouched.
  const startNewConversation = () => {
    closePanels();
    closeSecondaryPanels();
    closePreview();
    closeSidebar();
    showConversation(newId());
    prompt?.focus();
  };

  const openConversation = (id) => {
    closePreview();
    closeSidebar();
    if (id !== activeConversationId) {
      showConversation(id);
    }
  };

  if (newVideoButton) {
    newVideoButton.addEventListener("click", startNewConversation);
  }

  const clearImagePreview = () => {
    if (previewUrl) {
      URL.revokeObjectURL(previewUrl);
      previewUrl = null;
    }
    if (imageInput) {
      imageInput.value = "";
    }
    if (imagePreview) {
      imagePreview.hidden = true;
    }
    composer?.classList.remove("has-image");
    applyProfileLimits();
    if (previewImage) {
      previewImage.removeAttribute("src");
    }
    if (previewName) {
      previewName.textContent = "";
    }
  };

  const closePanels = () => {
    panelToggles.forEach((toggle) => {
      const panel = document.getElementById(toggle.getAttribute("aria-controls"));
      toggle.setAttribute("aria-expanded", "false");
      toggle.classList.remove("is-active");
      if (panel) {
        panel.hidden = true;
      }
    });
  };

  const closeSecondaryPanels = () => {
    secondaryToggles.forEach((toggle) => {
      const panel = document.getElementById(toggle.getAttribute("aria-controls"));
      toggle.setAttribute("aria-expanded", "false");
      toggle.setAttribute("aria-label", `Show ${toggle.getAttribute("aria-controls") === "generation-settings" ? "generation settings" : "prompt suggestions"}`);
      toggle.classList.remove("is-active");
      if (panel) {
        panel.hidden = true;
      }
    });
  };

  // Settings and Explore open in the same place above the composer, so only one
  // is open at a time.
  secondaryToggles.forEach((toggle) => {
    toggle.addEventListener("click", () => {
      const panel = document.getElementById(toggle.getAttribute("aria-controls"));
      const wasOpen = toggle.getAttribute("aria-expanded") === "true";
      if (!panel) {
        return;
      }

      if (!wasOpen) {
        closeSecondaryPanels();
      }
      toggle.setAttribute("aria-expanded", String(!wasOpen));
      toggle.setAttribute(
        "aria-label",
        `${wasOpen ? "Show" : "Hide"} ${toggle.getAttribute("aria-controls") === "generation-settings" ? "generation settings" : "prompt suggestions"}`
      );
      toggle.classList.toggle("is-active", !wasOpen);
      panel.hidden = wasOpen;
    });
  });

  panelToggles.forEach((toggle) => {
    toggle.addEventListener("click", () => {
      const panel = document.getElementById(toggle.getAttribute("aria-controls"));
      const wasOpen = toggle.getAttribute("aria-expanded") === "true";
      closePanels();
      if (!wasOpen && panel) {
        toggle.setAttribute("aria-expanded", "true");
        toggle.classList.add("is-active");
        panel.hidden = false;
      }
    });
  });

  panelSuggestions.forEach((suggestion) => {
    suggestion.addEventListener("click", () => {
      if (prompt && !isComposerLocked()) {
        prompt.value = suggestion.dataset.panelSuggestion || "";
        prompt.focus();
      }
    });
  });

  if (composerPlus && imageInput) {
    composerPlus.addEventListener("click", () => {
      imageInput.click();
    });
  }

  if (imageInput) {
    imageInput.addEventListener("change", () => {
      const file = imageInput.files && imageInput.files[0];
      if (!file) {
        return;
      }

      const allowedTypes = ["image/png", "image/jpeg", "image/webp"];
      if (!allowedTypes.includes(file.type)) {
        clearImagePreview();
        if (formMessage) {
          formMessage.textContent = "Please select a PNG, JPG, JPEG, or WebP image.";
          formMessage.classList.add("is-error");
        }
        return;
      }

      if (file.size > maxImageSize) {
        clearImagePreview();
        if (formMessage) {
          formMessage.textContent = "Please choose an image of 25 MB or smaller.";
          formMessage.classList.add("is-error");
        }
        return;
      }

      if (previewUrl) {
        URL.revokeObjectURL(previewUrl);
      }
      previewUrl = URL.createObjectURL(file);
      if (previewImage) {
        previewImage.src = previewUrl;
      }
      if (previewName) {
        previewName.textContent = file.name;
      }
      if (imagePreview) {
        imagePreview.hidden = false;
      }
      // Lets the composer wrap so the preview sits on its own line and the
      // prompt keeps its full width.
      composer?.classList.add("has-image");
      applyProfileLimits(); // image-to-video uses its own profile
      if (formMessage) {
        formMessage.textContent = "Image attached. Add or edit your prompt, then press send.";
        formMessage.classList.remove("is-error");
      }
    });
  }

  if (removeImage) {
    removeImage.addEventListener("click", () => {
      clearImagePreview();
      if (formMessage) {
        formMessage.textContent = "Source image removed.";
        formMessage.classList.remove("is-error");
      }
    });
  }

  suggestionCards.forEach((card) => {
    card.addEventListener("click", () => {
      if (prompt && !isComposerLocked()) {
        prompt.value = card.dataset.suggestion || "";
        prompt.focus();
      }
    });
  });

  settingInputs.forEach((input) => {
    input.addEventListener("change", () => {
      const settingName = input.dataset.setting;
      if (settingName && Object.prototype.hasOwnProperty.call(generationSettings, settingName)) {
        generationSettings[settingName] = input.value;
      }
    });
  });

  if (composerForm) {
    composerForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (isSubmitBlocked()) {
        noteOtherChatGenerating();
        return; // A request is being sent or one of this user's videos is still generating.
      }
      const promptText = prompt ? prompt.value.trim() : "";
      const hasImage = Boolean(imageInput?.files?.[0]);
      if (!formMessage) {
        return;
      }

      if (!promptText && !hasImage) {
        formMessage.textContent = "Describe your video idea or add an image before continuing.";
        formMessage.classList.add("is-error");
        if (prompt) {
          prompt.focus();
        }
        return;
      }

      // The API accepts at most 1000 characters after trimming, counted like Python
      // (an emoji is one character, hence code points, not UTF-16 units). Refuse a
      // longer prompt here, before a chat turn is created, rather than after a 400.
      const promptLength = [...promptText].length;
      if (promptLength > MAX_PROMPT_LENGTH) {
        formMessage.textContent = `Prompts can be at most ${MAX_PROMPT_LENGTH} characters. Yours has ${promptLength}; please shorten it.`;
        formMessage.classList.add("is-error");
        if (prompt) {
          prompt.focus();
        }
        return;
      }

      // The prompt becomes a turn of the open conversation; a new chat is saved now.
      const imageName = hasImage ? imageInput.files[0].name : "";
      const now = new Date().toISOString();
      let conversation = activeConversation();
      if (!conversation) {
        const title = promptText.replace(/\s+/g, " ").slice(0, 60) || `Image: ${imageName}`.slice(0, 60);
        conversation = { id: activeConversationId, title, pinned: false, createdAt: now, updatedAt: now, turns: [] };
        conversations.unshift(conversation);
      }
      const turn = {
        id: newId(),
        prompt: promptText.slice(0, MAX_PROMPT_LENGTH),
        imageName,
        mode: hasImage ? "image_to_video" : "text_to_video",
        jobId: null,
        status: "submitting",
        error: "",
        createdAt: now
      };
      conversation.turns.push(turn);
      conversation.turns.splice(0, Math.max(0, conversation.turns.length - MAX_TURNS));
      conversation.updatedAt = now;
      saveConversations();
      renderSidebarLists();

      submitting = true;
      updateComposerLock();
      formMessage.textContent = "Submitting your request…";
      formMessage.classList.remove("is-error");
      addUserMessage(turn.prompt, imageName);
      const generationMessage = addAssistantMessage();
      turnElements.set(turn.id, generationMessage);
      paintTurn(turn, generationMessage);
      scrollChatToEnd();
      try {
        const job = await submitGeneration(promptText);
        formMessage.textContent = hasImage && !promptText
          ? "Request accepted with your image and no text prompt. Your video is in the queue."
          : "Request accepted. Your video is in the queue.";
        // The prompt and image stay in place (read-only) while the video is
        // generated; the lock is released by the job status, not by this response.
        turn.jobId = job.job_id; // saved before polling starts, so results find their chat
        turn.status = job.status;
        saveConversations();
        renderSidebarLists();
        const accepted = turnElements.get(turn.id); // only if its chat is still open
        if (accepted?.isConnected) {
          paintTurn(turn, accepted);
        }
        trackJobStatus(job.job_id, job.status);
        pollJob(job.job_id);
        loadHistory();
      } catch (error) {
        formMessage.textContent = error.message;
        formMessage.classList.add("is-error");
        turn.status = "error";
        turn.error = error.message;
        saveConversations();
        renderSidebarLists();
        const message = turnElements.get(turn.id); // only if its chat is still open
        if (message?.isConnected) {
          paintTurn(turn, message); // the prompt stays in the composer
        }
        if (error.code === "generation_in_progress") {
          // The server has an active job this page did not know about (for
          // example one sent from another tab). History picks it up, polls it,
          // and Send waits until it finishes.
          loadHistory();
        }
      } finally {
        submitting = false;
        updateComposerLock();
      }
    });
  }

  // Restore the open conversation, and keep tracking jobs that were still
  // running when the page was last closed, so each result reaches its own chat.
  if (conversations.length) {
    saveConversations(); // stores any cleanup done while reading; nothing is written for a new visitor
  }
  renderActiveConversation();
  renderSidebarLists();
  conversations.forEach((conversation) => conversation.turns.forEach((turn) => {
    if (turn.jobId && ACTIVE_STATUSES.has(turn.status)) {
      trackJobStatus(turn.jobId, turn.status);
      pollJob(turn.jobId);
    }
  }));
  updateComposerLock();
  loadHistory();
  loadProfiles();

  if (prompt) {
    const resizePrompt = () => {
      prompt.style.height = "auto";
      prompt.style.height = `${Math.min(prompt.scrollHeight, 128)}px`;
    };

    prompt.addEventListener("input", resizePrompt);
    prompt.addEventListener("input", () => {
      if (prompt.value.trim() && formMessage && formMessage.classList.contains("is-error")) {
        formMessage.textContent = "";
        formMessage.classList.remove("is-error");
      }
    });
    prompt.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        if (typeof composerForm?.requestSubmit === "function") {
          composerForm.requestSubmit();
        } else if (composerForm) {
          composerForm.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
        }
      }
    });
    resizePrompt();
  }

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && body.classList.contains("sidebar-open")) {
      closeSidebar();
      if (sidebarToggle) {
        sidebarToggle.focus();
      }
      return;
    }
    // Escape closes an open Settings or Explore panel and returns focus to its button.
    const openToggle = [...secondaryToggles].find((toggle) => toggle.getAttribute("aria-expanded") === "true");
    if (event.key === "Escape" && openToggle) {
      closeSecondaryPanels();
      openToggle.focus();
    }
  });

  window.addEventListener("resize", () => {
    if (window.innerWidth > 900) {
      closeSidebar();
    }
  });
})();
