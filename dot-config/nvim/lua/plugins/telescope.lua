vim.pack.add({
	"https://github.com/nvim-telescope/telescope-symbols.nvim",
	"https://github.com/nvim-telescope/telescope-fzf-native.nvim",
	"https://github.com/nvim-telescope/telescope-file-browser.nvim",
	"https://github.com/nvim-telescope/telescope.nvim",
	"https://github.com/nvim-lua/plenary.nvim",
}, {
	confirm = false,
	callback = function()
		local telescope = require("telescope")
		local actions = require("telescope.actions")
		local config = require("telescope.config")

		local opts = {
			defaults = {
				mappings = {
					i = {
						["<Esc>"] = actions.close,
					},
				},
				-- Fallback pattern filtering
				file_ignore_patterns = {
					"bin/",
					"obj/",
					"%.git/",
					"node_modules/",
				},
			},
			extensions = {
				file_browser = {
					hijack_netrw = true,
					hidden = true,
					no_ignore = false, -- Changed to false to prevent showing ignored files here
				},
			},
		}

		if vim.fn.executable("rg") == 1 then
			-- Clean commands that completely respect your .gitignore rules
			local rg_find = { "rg", "--files", "--hidden", "--glob", "!**/.git/*" }
			local rg_grep = {
				"rg",
				"--color=never",
				"--no-heading",
				"--with-filename",
				"--line-number",
				"--column",
				"--smart-case",
				"--hidden",
				"--glob",
				"!**/.git/*",
			}

			local rg_scope = {
				defaults = { vimgrep_arguments = rg_grep },
				pickers = { find_files = { find_command = rg_find } },
			}
			opts = vim.tbl_deep_extend("force", opts, rg_scope)
		end

		telescope.setup(opts)
		telescope.load_extension("file_browser")

		local fzfutil = require("fzf-util")
		local fzfplugindir = vim.pack.get({ "telescope-fzf-native.nvim" })[1].path
		fzfutil.load(fzfplugindir)

		vim.api.nvim_create_autocmd("PackChanged", {
			callback = function(packOpts)
				if packOpts.data.spec.name == "telescope-fzf-native.nvim" and packOpts.data.kind == "update" then
					fzfutil.build(fzfplugindir, function()
						telescope.load_extension("fzf")
					end)
				end
			end,
		})
	end,
})

-- Keymaps
local vk = vim.keymap
vk.set("n", "<leader>ff", function()
	require("telescope.builtin").find_files({ hidden = false })
end, { silent = true, desc = "Find files" })
vk.set("n", "<leader>fg", ":Telescope live_grep<cr>", { silent = true, desc = "Live grep" })
vk.set("n", "<leader>fG", ":Telescope git_files<cr>", { silent = true, desc = "Git files" })
vk.set("n", "<leader>fb", ":Telescope buffers<cr>", { silent = true, desc = "Buffers" })
vk.set("n", "<leader>fr", ":Telescope oldfiles<cr>", { silent = true, desc = "Recent Files" })
vk.set("n", "<leader>fh", ":Telescope help_tags<cr>", { silent = true, desc = "Find Help" })
vk.set(
	"n",
	"<leader>fB",
	":Telescope file_browser path=%:p:h select_buffer=true<CR>",
	{ silent = true, desc = "File browser" }
)
