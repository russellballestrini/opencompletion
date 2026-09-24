// Code blocks: truncation, copy, run via Unsandbox, artifacts.
// Part of chat.html's script, split by concern; files load in order &
// share globals (see templates/chat.html). Config comes from CHAT_CONFIG.

function truncateCodeBlock(block, maxLines = 100) {
    // Split the content by new lines and check if it exceeds the maxLines
    const lines = block.textContent.split('\n');
    if (lines.length > maxLines) {
        // Store the full content in a data attribute
        block.dataset.fullContent = block.textContent;

        // Truncate the displayed content
        const truncatedText = lines.slice(0, maxLines).join('\n') + '\n...';
        block.textContent = truncatedText;

        // Store truncated text for Show More/Show Less toggle
        block.dataset.truncatedText = truncatedText;

        return true; // Indicate that the block was truncated
    }
    return false; // Indicate that the block was not truncated
}


// Modify the addCopyButtonToCodeBlock function to use the full content
function addCopyButtonToCodeBlock(block, wasTruncated = false) {
    // Check if the full content is stored in a data attribute, otherwise use textContent
    const contentToCopy = block.dataset.fullContent || block.textContent;

    // Create a container for the buttons
    const buttonContainer = document.createElement('div');
    buttonContainer.classList.add('code-block-button-container');
    buttonContainer.style.display = 'flex';
    buttonContainer.style.gap = '8px';
    buttonContainer.style.marginTop = '8px';

    // If truncated, add a "Show More" button
    if (wasTruncated) {
        const expandButton = document.createElement('button');
        expandButton.textContent = 'Show More';
        expandButton.classList.add('show-more-button');

        const truncatedText = block.dataset.truncatedText;

        expandButton.onclick = function() {
            // Restore the full content from the data attribute
            block.textContent = block.dataset.fullContent;
            // Reapply syntax highlighting
            hljs.highlightElement(block);
            addLineNumbers(block);
            // Change the button text to "Show Less"
            expandButton.textContent = 'Show Less';
            // Change the onclick function to truncate the block again
            expandButton.onclick = function() {
                block.textContent = truncatedText;
                // Reapply syntax highlighting
                hljs.highlightElement(block);
                addLineNumbers(block);
                // Change the button text back to "Show More"
                expandButton.textContent = 'Show More';
                // Set the onclick function back to the original expand function
                expandButton.onclick = originalExpandFunction;
            };
        };

        // Keep a reference to the original expand function
        const originalExpandFunction = expandButton.onclick;

        buttonContainer.appendChild(expandButton);
    }

    // Create a button to copy the code block's content
    const copyButton = document.createElement('button');
    copyButton.textContent = 'Copy';
    copyButton.classList.add('copy-button');
    copyButton.onclick = function() {
        // Copy the content to the clipboard
        navigator.clipboard.writeText(contentToCopy).then(() => {
            // Optionally, indicate that the text was copied
            copyButton.textContent = 'Copied!';
            setTimeout(() => {
                copyButton.textContent = 'Copy';
            }, 2000); // Reset button text after 2 seconds
        }).catch(err => {
            console.error('Error copying text: ', err);
        });
    };

    // Create a button to execute the code block's content
    const playButton = document.createElement('button');
    playButton.textContent = '▶ Run';
    playButton.classList.add('play-button');
    playButton.onclick = function() {
        executeCodeBlock(contentToCopy, block, playButton);
    };

    // Create a button to download compiled binary (hidden initially)
    const downloadBinaryButton = document.createElement('button');
    downloadBinaryButton.textContent = '⬇ Download Binary';
    downloadBinaryButton.classList.add('download-binary-button');
    downloadBinaryButton.style.display = 'none';

    // Add buttons to container
    buttonContainer.appendChild(copyButton);
    buttonContainer.appendChild(playButton);
    buttonContainer.appendChild(downloadBinaryButton);

    // Insert the button container after the <pre> element (not inside it)
    // block is <code>, block.parentNode is <pre>
    // We want to insert after <pre>, so we use <pre>.parentNode and <pre>.nextSibling
    const preElement = block.parentNode;
    preElement.parentNode.insertBefore(buttonContainer, preElement.nextSibling);
}

// Function to execute code block content
async function executeCodeBlock(code, blockElement, playButton) {
    // Update button state
    playButton.textContent = 'Running...';
    playButton.disabled = true;

    // Check if we already have a results container
    let resultsContainer = blockElement.parentNode.querySelector('.code-execution-results');
    if (!resultsContainer) {
        // Create results container
        resultsContainer = document.createElement('div');
        resultsContainer.classList.add('code-execution-results');
        resultsContainer.style.marginTop = '10px';
        resultsContainer.style.padding = '10px';
        resultsContainer.style.backgroundColor = 'var(--bg-code)';
        resultsContainer.style.borderRadius = '5px';
        resultsContainer.style.fontFamily = 'monospace';
        resultsContainer.style.fontSize = '14px';
        resultsContainer.style.whiteSpace = 'pre-wrap';
        resultsContainer.style.wordWrap = 'break-word';

        // Insert after the code block
        blockElement.parentNode.insertBefore(resultsContainer, blockElement.nextSibling);
    }

    // Check if this is an auto-exec from a fix and set the attempt counter
    if (blockElement.dataset.autoExecAttempt) {
        resultsContainer.dataset.fixAttempts = blockElement.dataset.autoExecAttempt;
        delete blockElement.dataset.autoExecAttempt; // Clean up after use
    }

    // Clear previous results
    resultsContainer.innerHTML = '<div style="color: var(--text-muted);">Executing code...</div>';

    try {
        // Try to detect language from the code block's class
        let language = null;
        const classes = blockElement.className.split(' ');
        for (const cls of classes) {
            if (cls.startsWith('language-')) {
                language = cls.replace('language-', '');
                break;
            }
        }

        // If no language specified (no class on code block), default to Python
        if (!language) {
            language = 'python';
        }

        // Use backend proxy for code execution (keeps API key secure)
        const asyncResponse = await fetch('/api/code/execute', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                language: language,
                code: code,
                return_artifact: true  // Request artifacts (binaries, images, videos, etc)
            })
        });

        if (!asyncResponse.ok) {
            // Show our server's reason (e.g. "Sign in to run code") when it gives one
            const failure = await asyncResponse.json().catch(() => ({}));
            throw new Error(failure.error || `HTTP error! status: ${asyncResponse.status}`);
        }

        const { job_id } = await asyncResponse.json();

        // Poll for results: 300ms, 750ms, 1450ms, 2350ms, 3000ms, 4600ms, 6600ms+
        const delays = [300, 450, 700, 900, 650, 1600, 2000];
        let pollCount = 0;
        let cancelButtonShown = false;

        // Create cancel button (hidden initially)
        let cancelButton = resultsContainer.querySelector('.cancel-execution-btn');
        if (!cancelButton) {
            cancelButton = document.createElement('button');
            cancelButton.textContent = 'Cancel';
            cancelButton.classList.add('cancel-execution-btn');
            cancelButton.style.display = 'none';
            cancelButton.style.marginTop = '8px';
            cancelButton.style.padding = '4px 8px';
            cancelButton.style.backgroundColor = 'var(--button-danger)';
            cancelButton.style.color = 'white';
            cancelButton.style.border = 'none';
            cancelButton.style.borderRadius = '3px';
            cancelButton.style.cursor = 'pointer';
            cancelButton.onclick = async () => {
                try {
                    await fetch(`/api/code/jobs/${job_id}`, { method: 'DELETE' });
                    cancelButton.disabled = true;
                    cancelButton.textContent = 'Cancelling...';
                } catch (error) {
                    console.error('Error cancelling job:', error);
                }
            };
            resultsContainer.appendChild(cancelButton);
        }

        while (true) {
            await sleep(delays[Math.min(pollCount, delays.length - 1)]);
            pollCount++;

            const jobResponse = await fetch(`/api/code/jobs/${job_id}`);
            if (!jobResponse.ok) {
                throw new Error(`Failed to fetch job status: ${jobResponse.status}`);
            }

            const job = await jobResponse.json();

            if (job.status !== 'pending' && job.status !== 'running') {
                // Job finished - hide cancel button
                if (cancelButton) {
                    cancelButton.style.display = 'none';
                }

                if (job.status === 'completed') {
                    // Unsandbox returns stdout/stderr/exit_code at top level of job response
                    displayExecutionResults(job, resultsContainer, language, code);

                    // Check if execution failed and attempt auto-fix
                    const exitCode = job.exit_code;
                    const stderr = job.stderr || '';

                    // Only use stderr for error detection (stdout/stderr properly separated now)
                    const shouldAutoFix = exitCode !== 0 && stderr.trim() !== '';

                    // Track attempts per code block (store in resultsContainer dataset)
                    if (!resultsContainer.dataset.fixAttempts) {
                        resultsContainer.dataset.fixAttempts = '0';
                    }

                    const currentAttempts = parseInt(resultsContainer.dataset.fixAttempts);

                    if (shouldAutoFix && currentAttempts < 3) {
                        // Show auto-fix message
                        const autoFixDiv = document.createElement('div');
                        autoFixDiv.style.color = 'var(--text-info)';
                        autoFixDiv.style.fontWeight = 'bold';
                        autoFixDiv.style.marginTop = '12px';
                        autoFixDiv.textContent = `Attempting to auto-fix errors (attempt ${currentAttempts + 1}/3)...`;
                        resultsContainer.appendChild(autoFixDiv);

                        // Increment attempt counter
                        resultsContainer.dataset.fixAttempts = (currentAttempts + 1).toString();

                        // Call backend to fix the code
                        try {
                            const fixResponse = await fetch('/api/fix-code', {
                                method: 'POST',
                                headers: {
                                    'Content-Type': 'application/json',
                                },
                                body: JSON.stringify({
                                    code: code,
                                    language: language,
                                    stderr: stderr,  // Send only stderr (properly separated now)
                                    exit_code: exitCode,
                                    attempt: currentAttempts + 1
                                })
                            });

                            if (fixResponse.ok) {
                                const fixData = await fixResponse.json();

                                if (fixData.success && fixData.fixed_code) {
                                    // Update status
                                    autoFixDiv.textContent = `Code fixed! Posting and re-executing (attempt ${currentAttempts + 1}/3)...`;

                                    // Store fixed code and attempt count for auto-execution after message is posted
                                    const autoExecData = {
                                        code: fixData.fixed_code,
                                        language: language,
                                        attempt: currentAttempts + 1
                                    };

                                    // Store in global variable so chat_message handler can access it
                                    window.pendingAutoExec = autoExecData;

                                    // Post the fixed code as a new message in the chat
                                    socket.emit("chat_message", {
                                        "username": username,
                                        "message": `**Auto-fixed code (attempt ${currentAttempts + 1}/3):**\n\n\`\`\`${language}\n${fixData.fixed_code}\n\`\`\``,
                                        "model": "None",
                                        "room_name": room_name
                                    });

                                    return; // Exit - the message handler will trigger execution
                                } else {
                                    autoFixDiv.textContent = `Auto-fix failed: ${fixData.error || 'Unknown error'}`;
                                    autoFixDiv.style.color = 'var(--text-error)';
                                }
                            } else {
                                autoFixDiv.textContent = `Auto-fix request failed (HTTP ${fixResponse.status})`;
                                autoFixDiv.style.color = 'var(--text-error)';
                            }
                        } catch (autoFixError) {
                            console.error('Error during auto-fix:', autoFixError);
                            autoFixDiv.textContent = `Auto-fix error: ${autoFixError.message}`;
                            autoFixDiv.style.color = 'var(--text-error)';
                        }
                    } else if (currentAttempts >= 3 && shouldAutoFix) {
                        // Max attempts reached
                        const maxAttemptsDiv = document.createElement('div');
                        maxAttemptsDiv.style.color = 'var(--text-warning)';
                        maxAttemptsDiv.style.fontWeight = 'bold';
                        maxAttemptsDiv.style.marginTop = '12px';
                        maxAttemptsDiv.textContent = 'Maximum auto-fix attempts (3) reached. Code still has errors.';
                        resultsContainer.appendChild(maxAttemptsDiv);
                    }

                    break;
                }

                // timeout, cancelled, or failed
                const errorMsg = job.error || job.status;

                // Display whatever output we have
                displayExecutionResults(job, resultsContainer, language, code);

                // Prepend error message to the results
                const errorDiv = document.createElement('div');
                errorDiv.style.color = 'var(--text-error)';
                errorDiv.style.fontWeight = 'bold';
                errorDiv.style.marginBottom = '8px';
                errorDiv.textContent = `Execution ${errorMsg}`;
                resultsContainer.insertBefore(errorDiv, resultsContainer.firstChild);

                break;
            }

            // Show cancel button after poll #5 (3000ms) if still running
            if (!cancelButtonShown && pollCount === 5) {
                cancelButtonShown = true;
                cancelButton.style.display = 'inline-block';
            }
        }

    } catch (error) {
        console.error('Error executing code:', error);
        resultsContainer.innerHTML = `<div style="color: var(--text-error);">Error: ${escapeHtml(error.message)}</div>`;
    } finally {
        // Reset button state
        playButton.textContent = '▶ Run';
        playButton.disabled = false;
    }
}

// Helper function to sleep
function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

// Helper function to display execution results
function displayExecutionResults(result, resultsContainer, language, code) {
    // Format and display results
    let outputHtml = '';

    // Unsandbox API returns flat structure: {success, stdout, stderr, exit_code, artifacts}
    const actualStdout = result.stdout || '';
    const actualStderr = result.stderr || '';
    const exitCode = result.exit_code;
    const artifacts = result.artifacts || [];

    // Show language
    if (language) {
        outputHtml += `<div style="color: var(--text-info); margin-bottom: 8px;">Language: ${escapeHtml(language)}</div>`;
    }

    // Show exit code
    if (exitCode !== undefined && exitCode !== null) {
        const exitColor = exitCode === 0 ? 'var(--text-success)' : 'var(--text-error)';
        outputHtml += `<div style="color: ${exitColor}; margin-bottom: 8px;">Exit Code: ${exitCode}</div>`;
    }

    // Show stdout
    if (actualStdout) {
        outputHtml += '<div style="color: var(--text-success); font-weight: bold;">Output:</div>';
        outputHtml += `<div style="color: var(--text-primary); margin-left: 10px;">${escapeHtml(actualStdout)}</div>`;
    }

    // Show stderr if present
    if (actualStderr) {
        outputHtml += '<div style="color: var(--text-error); font-weight: bold; margin-top: 8px;">Errors/Warnings:</div>';
        outputHtml += `<div style="color: var(--text-error); margin-left: 10px;">${escapeHtml(actualStderr)}</div>`;
    }

    // If no output at all
    if (!actualStdout && !actualStderr) {
        outputHtml += '<div style="color: var(--text-muted);">(No output produced)</div>';
    }

    resultsContainer.innerHTML = outputHtml;

    // Handle artifacts (binaries, images, videos, etc.)
    if (artifacts && artifacts.length > 0) {
        const artifactsDiv = document.createElement('div');
        artifactsDiv.style.marginTop = '12px';
        artifactsDiv.style.borderTop = '1px solid var(--border-color)';
        artifactsDiv.style.paddingTop = '12px';

        const artifactsTitle = document.createElement('div');
        artifactsTitle.style.color = 'var(--text-info)';
        artifactsTitle.style.fontWeight = 'bold';
        artifactsTitle.style.marginBottom = '8px';
        artifactsTitle.textContent = `Artifacts (${artifacts.length}):`;
        artifactsDiv.appendChild(artifactsTitle);

        artifacts.forEach((artifact, index) => {
            const artifactItem = document.createElement('div');
            artifactItem.style.marginBottom = '8px';
            artifactItem.style.padding = '8px';
            artifactItem.style.backgroundColor = 'var(--bg-secondary)';
            artifactItem.style.borderRadius = '4px';

            // Artifact name/filename
            const artifactName = document.createElement('div');
            artifactName.style.fontWeight = 'bold';
            artifactName.style.marginBottom = '4px';
            artifactName.textContent = artifact.filename || artifact.name || `Artifact ${index + 1}`;
            artifactItem.appendChild(artifactName);

            // Artifact type/size info
            const mimeType = artifact.mime_type || artifact.type || '';
            if (mimeType || artifact.size) {
                const artifactInfo = document.createElement('div');
                artifactInfo.style.fontSize = '12px';
                artifactInfo.style.color = 'var(--text-muted)';
                artifactInfo.style.marginBottom = '8px';
                let infoText = '';
                if (mimeType) infoText += `Type: ${mimeType}`;
                if (artifact.size) infoText += ` | Size: ${formatFileSize(artifact.size)}`;
                artifactInfo.textContent = infoText;
                artifactItem.appendChild(artifactInfo);
            }

            // Buttons container
            const buttonsDiv = document.createElement('div');
            buttonsDiv.style.display = 'flex';
            buttonsDiv.style.gap = '8px';

            // Determine if artifact is viewable (images, videos, text)
            const isImage = mimeType.startsWith('image/');
            const isVideo = mimeType.startsWith('video/');
            const isBinary = mimeType.includes('octet-stream') || mimeType.includes('executable');

            // Download button (always available)
            const downloadBtn = document.createElement('button');
            downloadBtn.textContent = '⬇ Download';
            downloadBtn.style.padding = '4px 8px';
            downloadBtn.style.fontSize = '12px';
            downloadBtn.onclick = () => downloadArtifact(artifact);
            buttonsDiv.appendChild(downloadBtn);

            // View button (disabled for binaries)
            const viewBtn = document.createElement('button');
            viewBtn.textContent = '👁 View';
            viewBtn.style.padding = '4px 8px';
            viewBtn.style.fontSize = '12px';
            if (isBinary) {
                viewBtn.disabled = true;
                viewBtn.style.opacity = '0.5';
                viewBtn.style.cursor = 'not-allowed';
            } else {
                viewBtn.onclick = () => viewArtifact(artifact, artifactItem, isImage, isVideo);
            }
            buttonsDiv.appendChild(viewBtn);

            artifactItem.appendChild(buttonsDiv);
            artifactsDiv.appendChild(artifactItem);
        });

        resultsContainer.appendChild(artifactsDiv);
    }

    // Find the download binary button (it's in the button container next to the Run button)
    // resultsContainer is inside <pre>, button container is the next sibling of <pre>
    const preElement = resultsContainer.parentNode;
    const buttonContainer = preElement.nextSibling;
    const downloadButton = buttonContainer?.querySelector('.download-binary-button');

    // Check for compiled binary artifact
    if (result.artifact && result.artifact.type === 'base64' && result.artifact.data) {
        // Show and populate the download button
        if (downloadButton) {
            downloadButton.style.display = 'inline-block';
            downloadButton.onclick = async () => {
                try {
                    // Generate AI filename if code is available
                    let filename = result.artifact.filename || 'compiled_binary';

                    if (code && language) {
                        try {
                            const nameResponse = await fetch('/api/generate-artifact-name', {
                                method: 'POST',
                                headers: {
                                    'Content-Type': 'application/json',
                                },
                                body: JSON.stringify({
                                    code: code,
                                    language: language
                                })
                            });

                            if (nameResponse.ok) {
                                const nameData = await nameResponse.json();
                                if (nameData.filename) {
                                    filename = nameData.filename;
                                }
                            }
                        } catch (nameError) {
                            // If naming fails, fall back to original filename
                            console.warn('Failed to generate AI filename:', nameError);
                        }
                    }

                    // Convert base64 to binary
                    const binaryString = atob(result.artifact.data);
                    const bytes = new Uint8Array(binaryString.length);
                    for (let i = 0; i < binaryString.length; i++) {
                        bytes[i] = binaryString.charCodeAt(i);
                    }

                    // Create blob and download
                    const blob = new Blob([bytes], { type: 'application/octet-stream' });
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = filename;
                    document.body.appendChild(a);
                    a.click();
                    document.body.removeChild(a);
                    URL.revokeObjectURL(url);
                } catch (error) {
                    console.error('Error downloading binary:', error);
                    alert('Failed to download binary: ' + error.message);
                }
            };
        }
    } else {
        // Hide the download button if no artifact or artifact error
        if (downloadButton) {
            downloadButton.style.display = 'none';
        }

        // Show artifact error if present
        if (result.artifact && result.artifact.type === 'error') {
            const artifactError = document.createElement('div');
            artifactError.style.color = 'var(--text-warning)';
            artifactError.style.marginTop = '8px';
            artifactError.style.fontSize = '12px';
            artifactError.textContent = `Artifact error: ${result.artifact.error}`;
            resultsContainer.appendChild(artifactError);
        }
    }
}

// Helper function to format file size
function formatFileSize(bytes) {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round((bytes / Math.pow(k, i)) * 100) / 100 + ' ' + sizes[i];
}

// Helper function to download artifact
async function downloadArtifact(artifact) {
    try {
        // Artifacts come as base64 in the response (field: content_base64)
        const base64Data = artifact.content_base64 || artifact.data || artifact.content;

        if (!base64Data) {
            console.error('No base64 data in artifact:', artifact);
            alert('Artifact data not available');
            return;
        }

        // Decode base64 to binary
        const binaryString = atob(base64Data);
        const bytes = new Uint8Array(binaryString.length);
        for (let i = 0; i < binaryString.length; i++) {
            bytes[i] = binaryString.charCodeAt(i);
        }

        // Determine mime type
        const mimeType = artifact.mime_type || artifact.type || 'application/octet-stream';

        // Create blob and download
        const blob = new Blob([bytes], { type: mimeType });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = artifact.filename || artifact.name || 'download';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    } catch (error) {
        console.error('Error downloading artifact:', error);
        alert('Failed to download artifact: ' + error.message);
    }
}

// Helper function to view artifact inline
async function viewArtifact(artifact, artifactItem, isImage, isVideo) {
    try {
        // Artifacts come as base64 in the response (field: content_base64)
        const base64Data = artifact.content_base64 || artifact.data || artifact.content;
        if (!base64Data) {
            console.error('No base64 data in artifact:', artifact);
            alert('Artifact data not available');
            return;
        }

        // Check if already viewing
        const existingViewer = artifactItem.querySelector('.artifact-viewer');
        if (existingViewer) {
            existingViewer.remove();
            return;
        }

        // Create viewer container
        const viewerDiv = document.createElement('div');
        viewerDiv.classList.add('artifact-viewer');
        viewerDiv.style.marginTop = '8px';
        viewerDiv.style.padding = '8px';
        viewerDiv.style.backgroundColor = 'var(--bg-primary)';
        viewerDiv.style.borderRadius = '4px';
        viewerDiv.style.maxWidth = '100%';
        viewerDiv.style.overflow = 'auto';

        // Determine mime type
        const mimeType = artifact.mime_type || artifact.type || 'application/octet-stream';

        if (isImage) {
            // Create data URL from base64
            const dataUrl = `data:${mimeType};base64,${base64Data}`;
            const img = document.createElement('img');
            img.src = dataUrl;
            img.style.maxWidth = '100%';
            img.style.height = 'auto';
            img.style.display = 'block';
            img.onerror = () => {
                viewerDiv.textContent = 'Failed to load image';
                viewerDiv.style.color = 'var(--text-error)';
            };
            viewerDiv.appendChild(img);
        } else if (isVideo) {
            // Create blob URL from base64
            const binaryString = atob(base64Data);
            const bytes = new Uint8Array(binaryString.length);
            for (let i = 0; i < binaryString.length; i++) {
                bytes[i] = binaryString.charCodeAt(i);
            }
            const blob = new Blob([bytes], { type: mimeType });
            const blobUrl = URL.createObjectURL(blob);

            const video = document.createElement('video');
            video.src = blobUrl;
            video.controls = true;
            video.style.maxWidth = '100%';
            video.style.height = 'auto';
            video.style.display = 'block';
            video.onerror = () => {
                viewerDiv.textContent = 'Failed to load video';
                viewerDiv.style.color = 'var(--text-error)';
                URL.revokeObjectURL(blobUrl);
            };
            viewerDiv.appendChild(video);
        } else {
            // For text types, decode and display
            try {
                const binaryString = atob(base64Data);
                const text = decodeURIComponent(escape(binaryString));
                const pre = document.createElement('pre');
                pre.style.margin = '0';
                pre.style.whiteSpace = 'pre-wrap';
                pre.style.wordWrap = 'break-word';
                pre.textContent = text;
                viewerDiv.appendChild(pre);
            } catch (decodeError) {
                viewerDiv.textContent = 'Failed to decode content';
                viewerDiv.style.color = 'var(--text-error)';
            }
        }

        artifactItem.appendChild(viewerDiv);
    } catch (error) {
        console.error('Error viewing artifact:', error);
        alert('Failed to view artifact: ' + error.message);
    }
}

// Helper function to escape HTML
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function addLineNumbers(block) {
    const lines = block.textContent.split('\n').length - 1;
    const lineNumbersWrapper = document.createElement('div');
    lineNumbersWrapper.className = 'line-numbers-rows';
    for (let i = 0; i < lines; i++) {
        lineNumbersWrapper.appendChild(document.createElement('span'));
    }
    block.appendChild(lineNumbersWrapper);
}
