# up

**up** is a short Python script for sharing files with others.
The script uploads files to a remote server and prints the files' public URLs.
Files are uploaded to the server using rsync.

Here is a typical use.
We upload `systemrescue+zfs-11.02+2.2.6-amd64.iso` and `systemrescue+zfs-11.02+2.2.6-amd64.iso.sha256` and get a download URL for each file:

```none
$ up systemrescue+zfs-11.02+2.2.6-amd64.iso systemrescue+zfs-11.02+2.2.6-amd64.iso.sha256
systemrescue+zfs-11.02+2.2.6-amd64.iso
    995.098.624 100%    5,46MB/s    0:02:53 (xfr#1, to-chk=1/2)
systemrescue+zfs-11.02+2.2.6-amd64.iso.sha256
            105 100%    0,27kB/s    0:00:00 (xfr#2, to-chk=0/2)
https://paste.example.com/1m17pnq-9bqsv/systemrescue%2Bzfs-11.02%2B2.2.6-amd64.iso
https://paste.example.com/1m17pnq-9bqsv/systemrescue%2Bzfs-11.02%2B2.2.6-amd64.iso.sha256
```

The directory name `1m17pnq-9bqsv` consists of current [Unix time](https://en.wikipedia.org/wiki/Unix_time) in [Crockford's base32](https://en.wikipedia.org/wiki/Base32#Crockford's_Base32) and five random base32 digits (25 bits of randomness) to prevent guessing.
Files uploaded simultaneously go in the same directory.

(The files in this example are from [SystemRescue+ZFS](https://github.com/nchevsky/systemrescue-zfs).)

## Requirements

- Python 3.11 or later for [tomllib](https://docs.python.org/3/library/tomllib.html)
- rsync 3.2.3 or later for `--mkpath`
- A remote machine with:
    - An SSH server
    - An HTTP server
      (or a server for another protocol that can assign URLs to files in subdirectories)

## Installation

```shell
# Install for the current user.
# You may need to add `~/.local/bin/` to your `PATH`.
mkdir -p ~/.local/bin/
install up ~/.local/bin/

# Or install for all users (replace `sudo` as necessary):
sudo install up /usr/local/bin/
```

## Configuration

`up` uses a TOML configuration file located at `$XDG_CONFIG_HOME/up/config.toml` (`~/.config/up/config.toml` by default).

If the configuration file is missing, the program will exit with an error message.

### Example

```toml
target_host = "example.com"
dest_dir = "/var/www/paste.example.com/"
base_url = "https://paste.example.com/"
```

### Keys

- `target_host`:
  The remote server host for rsync.
- `dest_dir`:
  The path to the destination directory on the remote server where files will be uploaded.
  Subdirectories will be created in this directory.
- `base_url`: The base URL corresponding to the remote `dest_dir`.
  The URL is constructed by joining the base URL, the subdirectory, and the [percent-encoded](https://en.wikipedia.org/wiki/Percent-encoding) filename with a forward slash.

## Usage

```none
usage: up [-h] file [file ...]

Upload files and print their URLs.

positional arguments:
  file        files to upload

options:
  -h, --help  show this help message and exit
```

## Server setup

Let's configure your web server on `paste.example.com` to serve the directory `/var/www/paste.example.com/`.
For [Caddy](https://github.com/caddyserver/caddy), you can use a Caddyfile like this:

```caddyfile
paste.example.com {
	root * /var/www/paste.example.com
	file_server {
		index index.txt
	}
	encode zstd gzip
	log

	handle_errors {
		@403 {
			expression {http.error.status_code} == 403
		}
		@404 {
			expression {http.error.status_code} == 404
		}
		@500 {
			expression {http.error.status_code} == 404
		}

		respond @403 "Permission denied."
		respond @404 "File not found."
		respond @500 "Internal server error."
	}
}
```

Create `/var/www/paste.example.com/index.txt` that says "Foo McBar's pastebin."

Ensure your remote user can write to `/var/www/paste.example.com` and Caddy can read from it.

## Motivation

The `up` script was inspired by [`asfa`](https://github.com/obreitwi/asfa), a tool that uploads files with a non-guessable hash-based prefix.
`asfa` does what `up` does and more.

I reimplemented the idea in `asfa` with several specific goals:

- Make the URLs easier to read and type and less prone to transcription errors
- Name subdirectories based on upload time for sorting without the uploader program
- Make the client a single script file for easy modification

## Q & A

### How can I list uploads on my server?

You can use tree(1) over SSH:

```none
$ ssh -qt example.com 'tree -CDFt --timefmt "%Y-%m-%d %H:%M %Z" /var/www/paste.example.com/'
[...]
└── [2025-05-01 20:33 UTC]  1m17pnq-9bqsv/
    ├── [2024-11-26 09:36 UTC]  systemrescue+zfs-11.02+2.2.6-amd64.iso
    └── [2024-11-26 09:36 UTC]  systemrescue+zfs-11.02+2.2.6-amd64.iso.sha256

16 directories, 24 files
```

### How do I delete uploaded files?

A command like the following lets you interactively choose files to delete.
You will need to install [fzf](https://github.com/junegunn/fzf) on the server.

```shell
ssh -qt example.com 'cd /var/www/paste.example.com/ && find -type f | fzf -m | xargs rm'
```

## License

[MIT License](LICENSE).
