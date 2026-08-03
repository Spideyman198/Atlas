/** @odoo-module **/

/**
 * The M11 acceptance criterion, as a tour.
 *
 *     a first-time user completes a question-to-cited-answer round trip
 *     without instructions
 *
 * So the tour is written as that user: it starts on an empty panel, uses only
 * what is visible on it, and finishes on the record the answer cited. Nothing
 * here reaches into component state or triggers a handler directly — a step
 * that cannot be performed by clicking is a step a first-time user cannot take.
 */

import { registry } from "@web/core/registry";

registry.category("web_tour.tours").add("atlas_chat_tour", {
    url: "/odoo/action-odoo_atlas.atlas_chat_action",
    steps: () => [
        {
            content: "The empty panel suggests something to ask",
            trigger: ".o_atlas_welcome .o_atlas_suggestion",
            run: "click",
        },
        {
            content: "The question appears as the user's own message",
            trigger: ".o_atlas_message.o_atlas_user .o_atlas_bubble",
        },
        {
            content: "The answer arrives",
            trigger: ".o_atlas_message.o_atlas_assistant .o_atlas_answer:contains(Acme)",
        },
        {
            content: "The answer cites the record it came from",
            trigger: ".o_atlas_citations .o_atlas_chip:contains(Acme)",
            run: "click",
        },
        {
            content: "Which opens that record",
            trigger: ".o_form_view .o_last_breadcrumb_item:contains(Acme)",
        },
    ],
});

/**
 * A second turn, to prove the panel keeps a conversation rather than a message.
 */
registry.category("web_tour.tours").add("atlas_chat_followup_tour", {
    url: "/odoo/action-odoo_atlas.atlas_chat_action",
    steps: () => [
        {
            content: "Ask the first question",
            trigger: ".o_atlas_composer textarea",
            run: "edit which orders are open?",
        },
        {
            trigger: ".o_atlas_send:not([disabled])",
            run: "click",
        },
        {
            content: "Wait for the answer",
            trigger: ".o_atlas_message.o_atlas_assistant .o_atlas_answer:contains(Acme)",
        },
        {
            content: "Ask a follow-up in the same conversation",
            trigger: ".o_atlas_composer textarea",
            run: "edit and the next one?",
        },
        {
            trigger: ".o_atlas_send:not([disabled])",
            run: "click",
        },
        {
            content: "Both questions are in one thread",
            trigger: ".o_atlas_thread .o_atlas_message.o_atlas_user:eq(1)",
        },
        {
            content: "And the conversation is listed in the sidebar",
            trigger: ".o_atlas_conversations .o_atlas_conversation",
        },
    ],
});
