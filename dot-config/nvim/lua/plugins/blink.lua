vim.pack.add({
         "https://github.com/saghen/blink.cmp"
}, { confirm = false })

require("blink.cmp").setup({
    sources = {
        default = { "lsp", "path", "snippets", "buffer" },
    },
    signature = { enabled = true },
    cmdline = {
        completion = { menu = { auto_show = true } },
    },
    completion = {
        documentation = { auto_show = true, auto_show_delay_ms = 150 },
    },
})
