vim.pack.add({
	"https://github.com/andrewferrier/wrapping.nvim",
}, { confirm = false })

opts = {
	set_nvim_opt_defaults = true,
	softener = {
		default = 1.0,
		markdown = 2.0,
		gitcommit = false, -- Based on https://stackoverflow.com/a/2120040/27641
	},
	create_commands = true,
	create_keymaps = true,
	auto_set_mode_heuristically = true,
	auto_set_mode_filetype_allowlist = {
		"asciidoc",
		"gitcommit",
		"help",
		"latex",
		"mail",
		"markdown",
		"rst",
		"tex",
		"text",
		"typst",
	},
	auto_set_mode_filetype_denylist = {},
	buftype_allowlist = {},
	excluded_treesitter_queries = {
		markdown = {
			"(fenced_code_block) @markdown1",
			"(atx_heading) @markdown2",
			"(pipe_table_header) @markdown3",
			"(pipe_table_delimiter_row) @markdown4",
			"(pipe_table_row) @markdown5",
		},
	},
	notify_on_switch = true,
}

require("wrapping").setup(opts)
