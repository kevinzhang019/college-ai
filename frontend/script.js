// API base URL — change this to your EC2 public IP for production
// e.g. 'http://12.34.56.78:8000'
const API_BASE_URL = window.COLLEGE_AI_API_URL || 'http://3.134.91.40:8000';

// College AI Assistant Frontend
class CollegeAI {
    constructor() {
        this.apiBaseUrl = API_BASE_URL;
        this.isConnected = false;
        this.collectionName = '';
        
        this.initializeElements();
        this.bindEvents();
        this.checkConnection();
    }

    initializeElements() {
        // Main elements
        this.questionInput = document.getElementById('questionInput');
        this.askButton = document.getElementById('askButton');
        this.collegeFilter = document.getElementById('collegeFilter');
        this.topKFilter = document.getElementById('topKFilter');
        
        // Searchable dropdown elements
        this.collegeDropdown = document.getElementById('collegeDropdown');
        this.collegeOptions = document.getElementById('collegeOptions');
        
        // Result elements
        this.loadingIndicator = document.getElementById('loadingIndicator');
        this.errorMessage = document.getElementById('errorMessage');
        this.errorText = document.getElementById('errorText');
        this.results = document.getElementById('results');
        this.answerContent = document.getElementById('answerContent');
        this.sourcesContent = document.getElementById('sourcesContent');
        
        // Status elements
        this.statusIndicator = document.getElementById('statusIndicator');
        this.statusText = document.getElementById('statusText');
        this.collectionInfo = document.getElementById('collectionInfo');
        
        // Modal elements
        this.helpButton = document.getElementById('helpButton');
        this.examplesModal = document.getElementById('examplesModal');
        this.closeModal = document.getElementById('closeModal');
    }

    bindEvents() {
        // Main interaction events
        this.askButton.addEventListener('click', () => this.handleAsk());
        
        // Global Enter key handler - submit from anywhere
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                // Special case: in question textarea, allow Shift+Enter for new lines
                if (e.target === this.questionInput && e.shiftKey) {
                    return; // Allow default behavior (new line)
                }
                
                // For all other cases, submit the prompt
                e.preventDefault();
                this.handleAsk();
            }
        });

        // Initialize searchable dropdowns
        this.initSearchableDropdown(this.collegeFilter, this.collegeDropdown, this.collegeOptions, 'colleges');

        // Modal events
        this.helpButton.addEventListener('click', () => this.showExamples());
        this.closeModal.addEventListener('click', () => this.hideExamples());
        this.examplesModal.addEventListener('click', (e) => {
            if (e.target === this.examplesModal) {
                this.hideExamples();
            }
        });

        // Example question clicks
        document.querySelectorAll('.example-category li').forEach(li => {
            li.addEventListener('click', () => {
                const question = li.textContent.replace(/^→\s*/, '').replace(/["""]/g, '');
                this.questionInput.value = question;
                this.hideExamples();
                this.questionInput.focus();
            });
        });

        // Auto-resize textarea
        this.questionInput.addEventListener('input', () => {
            this.questionInput.style.height = 'auto';
            this.questionInput.style.height = Math.min(this.questionInput.scrollHeight, 200) + 'px';
        });

        // Escape key to close modal
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && !this.examplesModal.classList.contains('hidden')) {
                this.hideExamples();
            }
        });
    }

    async checkConnection() {
        try {
            this.updateStatus('checking', 'Checking connection...');
            
            // Check health endpoint
            const healthResponse = await this.makeRequest('/health');
            if (healthResponse.status !== 'ok') {
                throw new Error('Health check failed');
            }

            // Get configuration info
            const configResponse = await this.makeRequest('/config');
            this.collectionName = configResponse.collection || 'Unknown';
            
            this.isConnected = true;
            this.updateStatus('online', 'Connected');
            this.collectionInfo.textContent = `Collection: ${this.collectionName}`;
            
            // Load filter options
            await this.loadFilterOptions();
            
        } catch (error) {
            this.isConnected = false;
            this.updateStatus('offline', 'Connection failed');
            this.collectionInfo.textContent = 'Collection: Unavailable';
            console.error('Connection check failed:', error);
        }
    }

    async loadFilterOptions() {
        try {
            const options = await this.makeRequest('/options');
            this.filterOptions = options;
            
            // Populate dropdowns
            this.populateDropdown(this.collegeOptions, options.colleges || []);
            
        } catch (error) {
            console.error('Failed to load filter options:', error);
            // Set comprehensive fallback options with more choices
            this.filterOptions = {
                colleges: [
                    'Stanford University',
                    'Harvard University',
                    'Massachusetts Institute of Technology',
                    'MIT',
                    'University of California—Berkeley',
                    'University of California—Los Angeles',
                    'UCLA',
                    'University of California—San Diego',
                    'University of Southern California',
                    'Yale University',
                    'Princeton University',
                    'Columbia University',
                    'University of Chicago',
                    'Northwestern University',
                    'Cornell University',
                    'University of Pennsylvania',
                    'Dartmouth College',
                    'Brown University',
                    'Duke University',
                    'Vanderbilt University',
                    'Rice University',
                    'Carnegie Mellon University',
                    'Georgia Institute of Technology',
                    'University of Michigan—Ann Arbor',
                    'University of Virginia',
                    'University of North Carolina—Chapel Hill',
                    'University of Texas—Austin',
                    'University of Washington',
                    'University of Wisconsin—Madison',
                    'University of Illinois—Urbana-Champaign',
                    'New York University',
                    'Boston University',
                    'Northeastern University',
                    'Rutgers University'
                ]
            };
            this.populateDropdown(this.collegeOptions, this.filterOptions.colleges);
        }
    }

    populateDropdown(container, options) {
        container.innerHTML = '';
        options.forEach(option => {
            const optionElement = document.createElement('div');
            optionElement.className = 'dropdown-option';
            optionElement.textContent = option;
            optionElement.addEventListener('click', () => {
                const input = container.previousElementSibling.previousElementSibling;
                input.value = option;
                container.parentElement.classList.remove('open');
            });
            container.appendChild(optionElement);
        });
    }

    initSearchableDropdown(input, dropdown, optionsContainer, optionType) {
        const arrow = dropdown.querySelector('.dropdown-arrow');
        let allOptions = [];
        
        // Store original options when they're loaded
        const observer = new MutationObserver(() => {
            if (optionsContainer.children.length > 0) {
                allOptions = Array.from(optionsContainer.children).map(el => el.textContent);
                observer.disconnect();
            }
        });
        observer.observe(optionsContainer, { childList: true });

        // Toggle dropdown on arrow click
        arrow.addEventListener('click', (e) => {
            e.stopPropagation();
            this.toggleDropdown(dropdown, optionsContainer, allOptions);
        });

        // Handle input changes for search
        input.addEventListener('input', (e) => {
            const searchTerm = e.target.value.toLowerCase();
            this.filterDropdownOptions(optionsContainer, allOptions, searchTerm);
            
            if (!dropdown.classList.contains('open')) {
                dropdown.classList.add('open');
            }
        });

        // Handle focus
        input.addEventListener('focus', () => {
            if (allOptions.length > 0) {
                this.filterDropdownOptions(optionsContainer, allOptions, input.value.toLowerCase());
                dropdown.classList.add('open');
            }
        });

        // Close dropdown when clicking outside
        document.addEventListener('click', (e) => {
            if (!dropdown.contains(e.target)) {
                dropdown.classList.remove('open');
            }
        });
    }

    toggleDropdown(dropdown, optionsContainer, allOptions) {
        const isOpen = dropdown.classList.contains('open');
        
        // Close all other dropdowns
        document.querySelectorAll('.searchable-dropdown.open').forEach(d => {
            if (d !== dropdown) d.classList.remove('open');
        });
        
        if (isOpen) {
            dropdown.classList.remove('open');
        } else {
            const input = dropdown.querySelector('input');
            this.filterDropdownOptions(optionsContainer, allOptions, input.value.toLowerCase());
            dropdown.classList.add('open');
        }
    }

    filterDropdownOptions(container, allOptions, searchTerm) {
        container.innerHTML = '';
        
        const filteredOptions = allOptions.filter(option =>
            option.toLowerCase().includes(searchTerm)
        );
        
        if (filteredOptions.length === 0) {
            const noOptionsElement = document.createElement('div');
            noOptionsElement.className = 'no-options';
            noOptionsElement.textContent = 'No options found';
            container.appendChild(noOptionsElement);
        } else {
            filteredOptions.forEach(option => {
                const optionElement = document.createElement('div');
                optionElement.className = 'dropdown-option';
                optionElement.textContent = option;
                
                // Highlight matching text
                if (searchTerm) {
                    const regex = new RegExp(`(${searchTerm})`, 'gi');
                    optionElement.innerHTML = option.replace(regex, '<strong>$1</strong>');
                }
                
                optionElement.addEventListener('click', () => {
                    const input = container.parentElement.querySelector('input');
                    input.value = option;
                    container.parentElement.classList.remove('open');
                });
                
                container.appendChild(optionElement);
            });
        }
    }

    updateStatus(status, message) {
        const statusDot = this.statusIndicator.querySelector('.status-dot');
        statusDot.className = `status-dot ${status}`;
        this.statusText.textContent = message;
    }

    async makeRequest(endpoint, options = {}) {
        const url = `${this.apiBaseUrl}${endpoint}`;
        const defaultOptions = {
            headers: {
                'Content-Type': 'application/json',
            },
        };
        
        const finalOptions = { ...defaultOptions, ...options };
        
        const response = await fetch(url, finalOptions);
        
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }
        
        return await response.json();
    }

    async handleAsk() {
        const question = this.questionInput.value.trim();
        
        if (!question) {
            this.showError('Please enter a question');
            return;
        }

        if (!this.isConnected) {
            this.showError('Not connected to the API server. Please check if the server is running.');
            return;
        }

        try {
            this.setLoading(true);
            this.hideError();
            this.hideResults();

            const payload = {
                question: question,
                top_k: parseInt(this.topKFilter.value),
            };

            // Add optional filters
            const college = this.collegeFilter.value.trim();
            if (college) payload.college = college;

            const response = await this.makeRequest('/ask', {
                method: 'POST',
                body: JSON.stringify(payload),
            });

            this.displayResults(response);
            
        } catch (error) {
            console.error('Ask request failed:', error);
            this.showError(`Failed to get response: ${error.message}`);
        } finally {
            this.setLoading(false);
        }
    }

    parseMarkdown(text) {
        if (!text) return '';
        
        // Escape HTML to prevent XSS
        let html = text
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
        
        // Split into lines for better processing
        const lines = html.split('\n');
        const processedLines = [];
        let inList = false;
        
        for (let i = 0; i < lines.length; i++) {
            let line = lines[i];
            
            // Headers (## text or ### text)
            if (line.match(/^### /)) {
                line = line.replace(/^### (.*)$/, '<h3>$1</h3>');
                if (inList) { processedLines.push('</ul>'); inList = false; }
            } else if (line.match(/^## /)) {
                line = line.replace(/^## (.*)$/, '<h2>$1</h2>');
                if (inList) { processedLines.push('</ul>'); inList = false; }
            } else if (line.match(/^# /)) {
                line = line.replace(/^# (.*)$/, '<h1>$1</h1>');
                if (inList) { processedLines.push('</ul>'); inList = false; }
            }
            // Bullet points (- item)
            else if (line.match(/^- /)) {
                if (!inList) {
                    processedLines.push('<ul>');
                    inList = true;
                }
                line = line.replace(/^- (.*)$/, '<li>$1</li>');
            }
            // Regular lines
            else {
                if (inList && line.trim() === '') {
                    // Empty line in list - keep list open
                } else if (inList && !line.match(/^- /)) {
                    // Non-bullet line after bullet - close list
                    processedLines.push('</ul>');
                    inList = false;
                }
                
                // Only wrap non-empty, non-header lines in paragraphs
                if (line.trim() !== '' && !line.match(/^<h[1-4]>/)) {
                    line = `<p>${line}</p>`;
                }
            }
            
            processedLines.push(line);
        }
        
        // Close any open list
        if (inList) {
            processedLines.push('</ul>');
        }
        
        html = processedLines.join('\n');
        
        // Apply text formatting
        html = html
            // Bold text (**text**)
            .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
            
            // Italic text (*text*)
            .replace(/\*([^*]+?)\*/g, '<em>$1</em>')
            
            // Citations [1], [2], etc.
            .replace(/\[(\d+)\]/g, '<span class="citation">[$1]</span>');
        
        return html;
    }

    displayResults(response) {
        const { answer, sources, confidence, source_count } = response;

        // Display confidence indicator
        const confidenceBanner = this.buildConfidenceBanner(confidence, source_count);

        // Display answer with markdown parsing
        this.answerContent.innerHTML = confidenceBanner + this.parseMarkdown(answer || 'No answer generated.');

        // Display sources
        this.sourcesContent.innerHTML = '';

        if (sources && sources.length > 0) {
            sources.forEach((source, index) => {
                const sourceElement = this.createSourceElement(source, index + 1);
                this.sourcesContent.appendChild(sourceElement);
            });
        } else {
            this.sourcesContent.innerHTML = '<p class="text-gray-500">No sources found.</p>';
        }

        this.showResults();
    }

    buildConfidenceBanner(confidence, sourceCount) {
        if (!confidence) return '';
        const configs = {
            high: { label: 'High confidence', icon: '\u2705', color: '#065f46', bg: '#d1fae5' },
            medium: { label: 'Moderate confidence', icon: '\u26A0\uFE0F', color: '#92400e', bg: '#fef3c7' },
            low: { label: 'Low confidence', icon: '\u26A0\uFE0F', color: '#991b1b', bg: '#fee2e2' },
        };
        const cfg = configs[confidence] || configs.medium;
        const countText = sourceCount ? ` \u2014 ${sourceCount} source${sourceCount !== 1 ? 's' : ''} found` : '';
        const verifyNote = confidence !== 'high' ? ' \u2014 verify with the college directly' : '';
        return `<div style="padding:8px 12px;margin-bottom:12px;border-radius:6px;background:${cfg.bg};color:${cfg.color};font-size:0.9em;">${cfg.icon} ${cfg.label}${countText}${verifyNote}</div>`;
    }

    createSourceElement(source, number) {
        const div = document.createElement('div');
        div.className = 'source-item';
        
        const college = source.college_name || 'Unknown College';
        const title = source.title || 'Untitled';
        const url = source.url || '#';
        const content = source.content || 'No content available';
        const distance = source.distance !== undefined ? source.distance.toFixed(3) : 'N/A';
        
        // Truncate content for display
        const maxContentLength = 300;
        const truncatedContent = content.length > maxContentLength 
            ? content.substring(0, maxContentLength) + '...'
            : content;
        
        div.innerHTML = `
            <div class="source-header">
                <div class="source-number">${number}</div>
                <div class="source-info">
                    <div class="source-title">${this.escapeHtml(title)}</div>
                    <div class="source-college">${this.escapeHtml(college)}</div>
                </div>
            </div>
            <a href="${this.escapeHtml(url)}" class="source-url" target="_blank" rel="noopener">
                ${this.escapeHtml(url)}
            </a>
            <div class="source-content">
                ${this.escapeHtml(truncatedContent)}
            </div>
        `;
        
        return div;
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    setLoading(isLoading) {
        if (isLoading) {
            this.askButton.disabled = true;
            this.loadingIndicator.classList.remove('hidden');
        } else {
            this.askButton.disabled = false;
            this.loadingIndicator.classList.add('hidden');
        }
    }

    showError(message) {
        this.errorText.textContent = message;
        this.errorMessage.classList.remove('hidden');
    }

    hideError() {
        this.errorMessage.classList.add('hidden');
    }

    showResults() {
        this.results.classList.remove('hidden');
    }

    hideResults() {
        this.results.classList.add('hidden');
    }

    showExamples() {
        this.examplesModal.classList.remove('hidden');
        document.body.style.overflow = 'hidden'; // Prevent background scrolling
    }

    hideExamples() {
        this.examplesModal.classList.add('hidden');
        document.body.style.overflow = ''; // Restore scrolling
    }

    // Utility method to check if API is available with custom URL
    async testConnection(customUrl) {
        const originalUrl = this.apiBaseUrl;
        this.apiBaseUrl = customUrl;
        
        try {
            await this.checkConnection();
            return this.isConnected;
        } catch (error) {
            this.apiBaseUrl = originalUrl;
            throw error;
        }
    }
}

// Initialize the application when DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
    window.collegeAI = new CollegeAI();
    
    // Add some helpful console messages
    console.log('🎓 College AI Assistant loaded!');
    console.log('💡 Tips:');
    console.log('  - Press Enter ANYWHERE to submit your question instantly!');
    console.log('  - Press Shift + Enter for new lines (in question field only)');
    console.log('  - Use the searchable dropdown for colleges');
    console.log('  - Click the help button (?) for example questions');
    
    // Check if API server might be running on a different port
    if (!window.collegeAI.isConnected) {
        console.log('⚠️  API server not found on port 8000.');
        console.log('   Make sure to start the server with:');
        console.log('   uvicorn college_ai.api.app:app --host 0.0.0.0 --port 8000 --reload');
    }
});

// Export for potential external use
if (typeof module !== 'undefined' && module.exports) {
    module.exports = CollegeAI;
}
