(() => {
  "use strict";

  const body = document.body;
  const sidebarToggle = document.querySelector("[data-sidebar-toggle]");
  const sidebarClose = document.querySelector("[data-sidebar-close]");
  const sidebarBackdrop = document.querySelector("[data-sidebar-backdrop]");
  const newVideoButton = document.querySelector("[data-new-video]");
  const prompt = document.querySelector("[data-prompt]");
  const composerForm = document.querySelector("[data-composer-form]");
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
  const maxImageSize = 10 * 1024 * 1024;
  let previewUrl = null;
  const generationSettings = {
    aspectRatio: document.querySelector("[data-setting='aspectRatio']:checked")?.value || "landscape",
    frameCount: document.querySelector("[data-setting='frameCount']:checked")?.value || "81",
    visualStyle: document.querySelector("[data-setting='visualStyle']:checked")?.value || "cinematic"
  };
  const resultsSection = document.querySelector("[data-results]");
  const resultsList = document.querySelector("[data-results-list]");
  const historyList = document.querySelector("[data-history-list]");
  const historyEmpty = document.querySelector("[data-history-empty]");
  const libraryList = document.querySelector("[data-library-list]");
  const libraryEmpty = document.querySelector("[data-library-empty]");
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
  const SIZES = { landscape: [832, 480], portrait: [480, 832] };
  const MODE_LABELS = { text_to_video: "Text to video", image_to_video: "Image to video" };
  const pollers = new Map();
  const cards = new Map();

  const apiRequest = async (path, options = {}) => {
    let response;
    try {
      response = await fetch(`${API_BASE}${path}`, { credentials: "same-origin", ...options });
    } catch (error) {
      throw new Error("Cannot reach the server. Is Django running?");
    }
    let data = null;
    try {
      data = await response.json();
    } catch (error) {
      data = null;
    }
    if (!response.ok) {
      throw new Error(data?.error?.message || `Request failed (${response.status}).`);
    }
    return data;
  };

  const buildSettings = () => {
    const [width, height] = SIZES[generationSettings.aspectRatio] || SIZES.landscape;
    return {
      width,
      height,
      frame_count: Number(generationSettings.frameCount),
      style: generationSettings.visualStyle
    };
  };

  const formatTime = (value) => (value ? new Date(value).toLocaleString() : "");

  const renderJobCard = (job) => {
    let card = cards.get(job.job_id);
    if (!card) {
      card = document.createElement("article");
      card.className = "job-card";
      cards.set(job.job_id, card);
      resultsList?.prepend(card);
    }
    if (resultsSection) {
      resultsSection.hidden = false;
    }

    const header = document.createElement("div");
    header.className = "job-card-header";
    const copy = document.createElement("div");
    const promptLine = document.createElement("p");
    promptLine.className = "job-prompt";
    promptLine.textContent = job.prompt || (job.mode === "image_to_video" ? "Animate the uploaded image" : "Untitled");
    const meta = document.createElement("p");
    meta.className = "job-meta";
    const size = job.settings ? ` · ${job.settings.width}×${job.settings.height} · ${job.settings.frame_count} frames` : "";
    meta.textContent = `${MODE_LABELS[job.mode] || job.mode}${size} · ${formatTime(job.created_at)}`;
    copy.append(promptLine, meta);
    const badge = document.createElement("span");
    badge.className = `job-status is-${job.status}`;
    badge.textContent = job.status;
    header.append(copy, badge);

    const children = [header];
    if (job.status === "completed" && job.result) {
      const actions = document.createElement("div");
      actions.className = "job-actions";
      const previewButton = document.createElement("button");
      previewButton.className = "job-preview-button";
      previewButton.type = "button";
      previewButton.textContent = "▶ Preview";
      previewButton.setAttribute("aria-controls", "video-preview");
      previewButton.addEventListener("click", () => selectPreview(job.job_id, job, { focus: true }));
      actions.append(previewButton);
      if (job.result.is_mock) {
        const mockTag = document.createElement("span");
        mockTag.className = "job-mock-tag";
        mockTag.textContent = "Mock test video";
        actions.append(mockTag);
      }
      children.push(actions);
    } else if (job.status === "failed" && job.error) {
      const error = document.createElement("p");
      error.className = "job-error";
      error.textContent = `${job.error.message} (ref ${job.error.request_id || "n/a"})`;
      children.push(error);
    }
    // Only rebuild the card when something visible changed.
    const signature = `${job.status}:${job.result?.preview_url || ""}`;
    if (card.dataset.signature !== signature) {
      card.dataset.signature = signature;
      card.replaceChildren(...children);
    }
  };

  const pollJob = (jobId) => {
    if (pollers.has(jobId)) {
      return;
    }
    const tick = async () => {
      try {
        const job = await apiRequest(`/generations/${jobId}/`);
        renderJobCard(job);
        if (jobId === previewJobId) {
          renderPreview(job);
        }
        if (job.status === "completed" || job.status === "failed") {
          clearInterval(pollers.get(jobId));
          pollers.delete(jobId);
          loadHistory();
        }
      } catch (error) {
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

  const loadHistory = async () => {
    try {
      const page = await apiRequest("/generations/?page_size=30");
      const items = page.results || [];
      historyList?.replaceChildren(...items.map(historyButton));
      if (historyEmpty) {
        historyEmpty.hidden = items.length > 0;
      }
      const finished = items.filter((item) => item.video_available);
      libraryList?.replaceChildren(...finished.map(historyButton));
      if (libraryEmpty) {
        libraryEmpty.hidden = finished.length > 0;
      }
      // Resume tracking of jobs that were still running when the page loaded.
      items
        .filter((item) => item.status === "queued" || item.status === "processing")
        .forEach((item) => pollJob(item.job_id));
    } catch (error) {
      if (historyEmpty) {
        historyEmpty.hidden = false;
        historyEmpty.textContent = error.message;
      }
    }
  };

  const submitGeneration = async (promptText) => {
    const file = imageInput?.files?.[0];
    const settings = buildSettings();
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
    previewDownloadName = `geetha-${isMock ? "mock-" : ""}${job.job_id}.mp4`;

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
      previewHeading?.focus();
      const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      previewPanel.scrollIntoView({ block: "nearest", behavior: reduceMotion ? "auto" : "smooth" });
    }
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
    } else {
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

  if (newVideoButton && prompt) {
    newVideoButton.addEventListener("click", () => {
      prompt.value = "";
      prompt.style.height = "auto";
      if (imagePreview && imagePreview.hidden === false) {
        clearImagePreview();
      }
      prompt.focus();
      if (formMessage) {
        formMessage.textContent = "";
        formMessage.classList.remove("is-error");
      }
      closePanels();
      closeSecondaryPanels();
      closePreview();
      closeSidebar();
    });
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

  secondaryToggles.forEach((toggle) => {
    toggle.addEventListener("click", () => {
      const panel = document.getElementById(toggle.getAttribute("aria-controls"));
      const wasOpen = toggle.getAttribute("aria-expanded") === "true";
      if (!panel) {
        return;
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
      if (prompt) {
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
          formMessage.textContent = "Please choose an image smaller than 10 MB.";
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
      if (formMessage) {
        formMessage.textContent = "Image preview ready. It has not been uploaded.";
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
      if (prompt) {
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

      if (submitButton) {
        submitButton.disabled = true;
      }
      formMessage.textContent = "Submitting your request…";
      formMessage.classList.remove("is-error");
      try {
        const job = await submitGeneration(promptText);
        formMessage.textContent = "Request accepted. Your video is in the queue.";
        if (prompt) {
          prompt.value = "";
          prompt.style.height = "auto";
        }
        clearImagePreview();
        pollJob(job.job_id);
        loadHistory();
      } catch (error) {
        formMessage.textContent = error.message;
        formMessage.classList.add("is-error");
      } finally {
        if (submitButton) {
          submitButton.disabled = false;
        }
      }
    });
  }

  loadHistory();

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
    }
  });

  window.addEventListener("resize", () => {
    if (window.innerWidth > 900) {
      closeSidebar();
    }
  });
})();
