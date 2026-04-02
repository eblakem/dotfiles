vim.pack.add({
    "https://github.com/declancm/cinnamon.nvim", -- smooth scrolling
}, { confirm = false })

require("cinnamon").setup({
    keymaps = {
        basic = true,
        extra = true,
    },
    options = { mode = "window" },
})
