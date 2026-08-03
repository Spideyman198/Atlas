/** @odoo-module **/

/**
 * The chat panel.
 *
 * One component holds the state and three render it. The state is deliberately
 * plain: a list of conversations, the messages of the open one, and whichever
 * answer is currently arriving. Anything cleverer would have to be kept in step
 * with a stream that can fail halfway through.
 *
 * The answer is read as it arrives. `/atlas/chat/ask` returns server-sent
 * events, and this reads the body with a stream reader rather than EventSource
 * because EventSource cannot issue a POST — and the question, the conversation
 * and the CSRF token all have to go in the body.
 */

import { Component, onMounted, useRef, useState, markup } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { escape } from "@web/core/utils/strings";

/** Keeps the transcript pinned to the newest message while an answer arrives. */
function scrollToEnd(ref) {
    if (ref.el) {
        ref.el.scrollTop = ref.el.scrollHeight;
    }
}

export class AtlasChat extends Component {
    static template = "odoo_atlas.AtlasChat";
    static props = {
        action: { type: Object, optional: true },
        actionId: { type: [Number, Boolean], optional: true },
        className: { type: String, optional: true },
        updateActionState: { type: Function, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.thread = useRef("thread");
        this.input = useRef("input");

        this.state = useState({
            conversations: [],
            conversationId: null,
            messages: [],
            suggestions: [],
            search: "",
            draft: "",
            /** The answer currently arriving, or null between turns. */
            pending: null,
            /** Set when a turn failed, so the composer can offer a retry. */
            error: null,
            /** The question that failed, kept so retry does not need retyping. */
            retryable: null,
            loading: true,
        });

        onMounted(async () => {
            await Promise.all([this.loadConversations(), this.loadSuggestions()]);
            this.state.loading = false;
            this.focusComposer();
        });
    }

    // -- loading ------------------------------------------------------------

    async loadConversations() {
        this.state.conversations = await this.orm.searchRead(
            "atlas.conversation",
            [["state", "!=", "archived"]],
            ["id", "name", "last_activity", "message_count"],
            { limit: 50, order: "last_activity desc" }
        );
    }

    async loadSuggestions() {
        this.state.suggestions = await this.orm.call(
            "atlas.conversation",
            "atlas_suggestions",
            []
        );
    }

    async openConversation(conversationId) {
        if (this.state.pending) {
            return;
        }
        this.state.conversationId = conversationId;
        this.state.error = null;
        this.state.retryable = null;
        this.state.messages = await this.orm.call(
            "atlas.conversation",
            "atlas_transcript",
            [[conversationId]]
        );
        this.focusComposer();
    }

    startNewConversation() {
        if (this.state.pending) {
            return;
        }
        this.state.conversationId = null;
        this.state.messages = [];
        this.state.error = null;
        this.state.retryable = null;
        this.focusComposer();
    }

    // -- asking -------------------------------------------------------------

    get canSend() {
        return Boolean(this.state.draft.trim()) && !this.state.pending;
    }

    get filteredConversations() {
        const needle = this.state.search.trim().toLowerCase();
        if (!needle) {
            return this.state.conversations;
        }
        return this.state.conversations.filter((c) =>
            (c.name || "").toLowerCase().includes(needle)
        );
    }

    onComposerKeydown(event) {
        // Enter sends, Shift+Enter breaks the line. The other way round makes a
        // chat panel feel like a form.
        if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            this.send();
        }
    }

    useSuggestion(question) {
        this.state.draft = question;
        this.send();
    }

    retry() {
        const question = this.state.retryable;
        if (!question) {
            return;
        }
        this.state.error = null;
        this.state.retryable = null;
        // The failed question was already stored against the conversation, so
        // the transcript is reloaded rather than adding a second copy of it.
        this.state.draft = question;
        this.send({ replayed: true });
    }

    async send({ replayed = false } = {}) {
        const question = this.state.draft.trim();
        if (!question || this.state.pending) {
            return;
        }

        this.state.draft = "";
        this.state.error = null;
        this.state.retryable = null;
        if (!replayed) {
            this.state.messages.push({
                id: `local-${Date.now()}`,
                role: "user",
                content: question,
                status: "done",
                citations: [],
            });
        }
        this.state.pending = { role: "assistant", content: "", citations: [], refused: false };
        scrollToEnd(this.thread);

        try {
            await this.streamAnswer(question);
        } catch (error) {
            // A dropped connection, not a refusal. The distinction matters: one
            // is worth retrying and the other is the answer.
            this.state.error = _t("The connection was lost before the answer arrived.");
            this.state.retryable = question;
            this.state.pending = null;
        }
        await this.afterAnswer();
    }

    async streamAnswer(question) {
        const body = new FormData();
        body.append("csrf_token", odoo.csrf_token);
        body.append(
            "payload",
            JSON.stringify({ question, conversation_id: this.state.conversationId })
        );

        const response = await fetch("/atlas/chat/ask", { method: "POST", body });
        if (!response.ok || !response.body) {
            throw new Error(`chat request failed with ${response.status}`);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
            const { done, value } = await reader.read();
            if (done) {
                break;
            }
            buffer += decoder.decode(value, { stream: true });
            // Events are separated by a blank line. Anything after the last one
            // is a partial event and stays in the buffer.
            const blocks = buffer.split("\n\n");
            buffer = blocks.pop();
            for (const block of blocks) {
                this.handleEvent(block);
            }
        }
    }

    handleEvent(block) {
        let kind = null;
        let data = null;
        for (const line of block.split("\n")) {
            if (line.startsWith("event:")) {
                kind = line.slice(6).trim();
            } else if (line.startsWith("data:")) {
                try {
                    data = JSON.parse(line.slice(5).trim());
                } catch {
                    data = null;
                }
            }
        }
        if (!kind || !data) {
            return;
        }

        if (kind === "open") {
            this.state.conversationId = data.conversation_id;
        } else if (kind === "delta") {
            this.state.pending.content += data.text || "";
            scrollToEnd(this.thread);
        } else if (kind === "done") {
            this.state.pending.content = data.text || this.state.pending.content;
            this.state.pending.citations = data.citations || [];
            this.state.pending.refused = Boolean(data.refused);
        } else if (kind === "error") {
            this.state.error = data.message || _t("The assistant could not answer.");
            this.state.retryable = null;
        }
    }

    async afterAnswer() {
        const pending = this.state.pending;
        if (pending && pending.content) {
            this.state.messages.push({
                id: `local-answer-${Date.now()}`,
                role: "assistant",
                content: pending.content,
                status: "done",
                citations: pending.citations,
                refused: pending.refused,
            });
        }
        this.state.pending = null;
        scrollToEnd(this.thread);
        await this.loadConversations();
        this.focusComposer();
    }

    focusComposer() {
        if (this.input.el) {
            this.input.el.focus();
        }
    }

    // -- rendering ----------------------------------------------------------

    /**
     * Render an answer, keeping its paragraphs and marking its citations.
     *
     * The text is escaped first and markup applied second. An answer quotes
     * customer records, and a record whose name contains a tag is a record, not
     * an attack — but it renders as one if the escaping is skipped.
     */
    formatAnswer(message) {
        const cited = new Set((message.citations || []).map((c) => String(c.sequence)));
        const html = escape(message.content || "")
            .toString()
            .replace(/\[(\d{1,3})\]/g, (match, number) =>
                cited.has(number)
                    ? `<sup class="o_atlas_marker" data-sequence="${number}">${number}</sup>`
                    : match
            )
            .replace(/\n{2,}/g, "</p><p>")
            .replace(/\n/g, "<br/>");
        return markup(`<p>${html}</p>`);
    }

    openCitation(citation) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: citation.res_model,
            res_id: citation.res_id,
            views: [[false, "form"]],
            target: "current",
        });
    }
}

registry.category("actions").add("atlas.chat", AtlasChat);
