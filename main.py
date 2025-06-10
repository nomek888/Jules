import logging
import os
import re
import asyncio
import ffmpeg # type: ignore
import html # For escaping HTML in URLs shown to user

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import TelegramError, NetworkError, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler # Added CallbackQueryHandler
from typing import Optional # For Optional type hint

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = "7639447946:AAHzVWhmRA184lRYoQk44T_kyM4anupgx2s"
OUTPUT_FILENAME = "video.mp4"

async def read_progress(stderr_pipe, total_duration_ms: float, update: Update, context: ContextTypes.DEFAULT_TYPE, progress_message_id: int):
    logger.debug(f"read_progress started for message_id {progress_message_id}. Total duration: {total_duration_ms}ms")
    last_reported_percentage = -1
    if stderr_pipe is None:
        logger.warning("stderr_pipe is None in read_progress. Cannot read progress.")
        return
    async for line_bytes in stderr_pipe:
        line = line_bytes.decode('utf-8', errors='ignore')
        logger.debug(f"ffmpeg stderr: {line.strip()}")
        current_ms = None
        time_match = re.search(r"out_time_ms=(\d+)", line)
        if time_match:
            current_ms = int(time_match.group(1)) / 1000
        else:
            time_match_hhmmss = re.search(r"time=(\d{2}):(\d{2}):(\d{2})\.(\d{2,3})", line)
            if time_match_hhmmss:
                hours, minutes, seconds, ms_fraction = map(int, time_match_hhmmss.groups())
                current_ms = (hours * 3600 + minutes * 60 + seconds) * 1000 + ms_fraction * (10 if len(str(ms_fraction)) == 2 else 1)
        if current_ms is not None and total_duration_ms > 0:
            percentage = int((current_ms / total_duration_ms) * 100)
            if percentage > last_reported_percentage and (percentage % 5 == 0 or percentage >= 99):
                try:
                    logger.debug(f"Updating progress for message_id {progress_message_id}: {percentage}%")
                    await context.bot.edit_message_text(
                        chat_id=update.effective_chat.id, message_id=progress_message_id, text=f"Downloading: {percentage}%"
                    )
                    last_reported_percentage = percentage
                except TelegramError as e:
                    logger.warning(f"TelegramError updating progress message for message_id {progress_message_id}: {e}")
                except Exception as e:
                    logger.exception(f"Unexpected error updating progress message for message_id {progress_message_id}")
        if "progress=end" in line:
            logger.info(f"ffmpeg progress indicated end for message_id {progress_message_id}.")
            break
    logger.debug(f"read_progress finished for message_id {progress_message_id}.")

async def download_video(url: str, update: Update, context: ContextTypes.DEFAULT_TYPE,
                         duration_limit_seconds: int = 0,
                         original_message_id: Optional[int] = None,
                         user_friendly_duration_str: Optional[str] = None) -> None: # Added user_friendly_duration_str
    """
    Downloads video from M3U8 URL, tracks progress, and uploads it.
    Accepts duration_limit_seconds, original_message_id, and user_friendly_duration_str.
    The full logic for using these parameters will be in the next subtask.
    """
    chat_id_to_use = None
    progress_message_id_to_use = None

    if update.callback_query: # Called from callback
        if not original_message_id:
            logger.error("download_video called from callback_query without original_message_id.")
            return # Or handle error appropriately
        chat_id_to_use = update.effective_chat.id
        progress_message_id_to_use = original_message_id # This is the message we'll be editing
        logger.info(f"download_video initiated from callback. Chat: {chat_id_to_use}, Original Msg ID: {progress_message_id_to_use}, URL: {url}, Duration: {duration_limit_seconds}s")
    elif update.message: # Direct call (e.g., if /record was not showing buttons)
        chat_id_to_use = update.effective_chat.id
        # For a direct call, we create a new progress message
        try:
            progress_message = await update.message.reply_text("Preparing to download...")
            progress_message_id_to_use = progress_message.message_id
        except TelegramError as e:
            logger.exception(f"Failed to send initial progress message for direct call to chat_id {chat_id_to_use}")
            return
        logger.info(f"download_video initiated directly. Chat: {chat_id_to_use}, New Msg ID: {progress_message_id_to_use}, URL: {url}, Duration: {duration_limit_seconds}s")
    else:
        logger.error("download_video called without callback_query or message.")
        return

    # For this subtask, we are just adapting the signature.
    # The actual use of duration_limit_seconds and intelligent message handling
    # based on original_message_id will be fleshed out in the *next* subtask.
    # For now, let's assume it uses progress_message_id_to_use for edits.
    # The rest of the function remains similar but should use chat_id_to_use and progress_message_id_to_use.

    # --- The following logic implements the duration limit and refined message/file handling ---

    chat_id = chat_id_to_use
    progress_message_id = progress_message_id_to_use

    # Use a unique filename if called from callback to potentially support concurrent operations,
    # otherwise use the standard OUTPUT_FILENAME. This could be simplified if concurrency isn't a goal.
    # For now, let's stick to a single OUTPUT_FILENAME to simplify cleanup and reduce disk usage over time
    # if unique filenames weren't cleaned up properly. If concurrency becomes an issue, this can be revisited.
    # current_output_filename = f"{os.path.splitext(OUTPUT_FILENAME)[0]}_{progress_message_id}{os.path.splitext(OUTPUT_FILENAME)[1]}" if original_message_id else OUTPUT_FILENAME
    current_output_filename = OUTPUT_FILENAME # Sticking to one filename for now.

    probed_total_duration_ms = 0.0 # Duration from ffmpeg probe
    download_success = False

    # Construct initial message text using user_friendly_duration_str
    duration_prefix = ""
    if user_friendly_duration_str and user_friendly_duration_str != "Unlimited":
        duration_prefix = f"Recording for {user_friendly_duration_str}. "
    elif user_friendly_duration_str == "Unlimited":
        duration_prefix = "Recording (Unlimited). "
    else: # Fallback if not provided, though it should be
        duration_prefix = "Recording. "


    try:
        logger.info(f"Probing URL: {url} for chat_id {chat_id}, message_id {progress_message_id}. Duration limit: {duration_limit_seconds}s. Friendly duration: {user_friendly_duration_str}")
        await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text=f"{duration_prefix}Inspecting video stream...")

        probe_info = await asyncio.to_thread(ffmpeg.probe, url, timeout=30)
        if 'format' in probe_info and 'duration' in probe_info['format']:
            duration_str = probe_info['format']['duration']
            if duration_str and duration_str != "N/A":
                probed_total_duration_ms = float(duration_str) * 1000
                logger.info(f"Probed video duration: {probed_total_duration_ms / 1000}s for {url}")

        if duration_limit_seconds == 0 and probed_total_duration_ms <= 0:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text=f"{duration_prefix}Could not determine video duration. Progress updates may be unavailable.")
            logger.warning(f"Probed duration unknown for {url}, and recording is {user_friendly_duration_str or 'unlimited'}.")
        elif duration_limit_seconds > 0:
             logger.info(f"Recording will be limited to {duration_limit_seconds}s ({user_friendly_duration_str}).")
        # If duration_limit_seconds == 0 but probed_total_duration_ms > 0, it's unlimited but we know the stream length.

    except ffmpeg.Error as e:
        stderr_output = e.stderr.decode('utf8', errors='ignore') if e.stderr else "No stderr."
        logger.error(f"ffmpeg probe error for {url} (recording {user_friendly_duration_str or 'N/A'}): {stderr_output}")
        user_message = f"Error inspecting video stream (for {user_friendly_duration_str or 'unlimited'} recording). The URL might be invalid or the stream format is not supported.\nDetails: {stderr_output[:200]}"
        try: await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text=user_message, reply_markup=None)
        except TelegramError: logger.warning(f"Failed to send probe error message to chat_id {chat_id}")
        return
    except asyncio.TimeoutError:
        logger.error(f"ffmpeg probe timed out for {url}")
        user_message = "Error inspecting video stream: The server took too long to respond. Please try again later."
        try: await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text=user_message, reply_markup=None)
        except TelegramError: logger.warning(f"Failed to send probe timeout message to chat_id {chat_id}")
        return
    except Exception:
        logger.exception(f"Unexpected error during probing of {url}")
        user_message = "An unexpected error occurred while trying to inspect the video. Please try again."
        try: await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text=user_message, reply_markup=None)
        except TelegramError: logger.warning(f"Failed to send unexpected probe error message to chat_id {chat_id}")
        return

    # Determine duration for ffmpeg process timeout calculation and for progress reporting
    effective_duration_for_timeout_ms = 0
    if duration_limit_seconds > 0:
        effective_duration_for_timeout_ms = duration_limit_seconds * 1000
    elif probed_total_duration_ms > 0: # Unlimited recording, but probed duration is known
        effective_duration_for_timeout_ms = probed_total_duration_ms
    # If both are 0 (unlimited and unknown length), timeout will use a large default.

    ffmpeg_process_timeout = 0
    if effective_duration_for_timeout_ms > 0:
        ffmpeg_process_timeout = int((effective_duration_for_timeout_ms / 1000) * 1.5) + 60 # 50% margin + 60s buffer
    else:
        ffmpeg_process_timeout = 7200 # Default 2 hours for truly unknown/unlimited length
    logger.info(f"Setting ffmpeg process timeout to {ffmpeg_process_timeout}s.")

    # Duration to be used for progress percentage calculation by read_progress
    # If limit is set, use that. Otherwise, use probed duration (which could be 0 if unknown).
    duration_for_progress_ms = (duration_limit_seconds * 1000) if duration_limit_seconds > 0 else probed_total_duration_ms

    process = None
    try:
        progress_text_prefix = f"Recording for {user_friendly_duration_str}" if user_friendly_duration_str and user_friendly_duration_str != "Unlimited" else "Recording (Unlimited)"
        if not duration_for_progress_ms > 0 : # If no duration for progress, don't show 0%
             await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text=f"{progress_text_prefix}...")
        else:
             await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text=f"{progress_text_prefix}: 0%")

        if os.path.exists(current_output_filename):
            logger.info(f"Removing existing file: {current_output_filename} before download.")
            os.remove(current_output_filename)

        logger.info(f"Starting ffmpeg download for {url} to {current_output_filename}. Effective duration for ffmpeg -t: {duration_limit_seconds if duration_limit_seconds > 0 else 'unlimited'}")

        input_options = {'format': 'hls', 'live_start_index': '-3'} # Keep existing input options
        output_options = {'c': 'copy', 'progress': 'pipe:1'}
        if duration_limit_seconds > 0:
            output_options['t'] = str(duration_limit_seconds)
            logger.info(f"Applying -t {duration_limit_seconds} to ffmpeg command.")

        stream = ffmpeg.input(url, **input_options)
        stream = ffmpeg.output(stream, current_output_filename, **output_options)

        process = await asyncio.to_thread(
            stream.run_async, pipe_stdin=False, pipe_stdout=False, pipe_stderr=True, quiet=True
        )
        logger.info(f"ffmpeg process started for {url}. PID: {process.process.pid if process.process else 'N/A'}. Using duration for progress: {duration_for_progress_ms/1000}s. Friendly duration: {user_friendly_duration_str}")
        if process.stderr:
            await read_progress(process.stderr, duration_for_progress_ms, update, context, progress_message_id, user_friendly_duration_str) # Pass friendly duration

        await asyncio.wait_for(asyncio.shield(process.wait()), timeout=ffmpeg_process_timeout)
        return_code = process.process.returncode if process.process else -1

        if return_code == 0:
            logger.info(f"Video downloaded successfully: {current_output_filename} from {url}")
            download_success = True
        else:
            # Attempt to get stderr from process object if available, even if already read by read_progress (it might contain final errors)
            # This part is tricky as stderr pipe might be closed/consumed.
            # For now, rely on read_progress for detailed ffmpeg errors during run, and this for generic failure.
            logger.error(f"ffmpeg download process for {url} (recording {user_friendly_duration_str or 'N/A'}) failed. Return code: {return_code}.")
            await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text=f"Download failed (for {user_friendly_duration_str or 'unlimited'} recording). FFmpeg error (see logs for details).", reply_markup=None)
    except ffmpeg.Error as e: # This catches errors from stream.run_async() if ffmpeg command itself fails (e.g. invalid options)
        stderr_output = e.stderr.decode('utf8', errors='ignore') if e.stderr else "No stderr available."
        logger.error(f"ffmpeg execution error for {url} (recording {user_friendly_duration_str or 'N/A'}): {stderr_output}")
        await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text=f"Error during download setup (for {user_friendly_duration_str or 'unlimited'} recording): {stderr_output[:200]}", reply_markup=None)
    except asyncio.TimeoutError:
        logger.error(f"ffmpeg download process for {url} (recording {user_friendly_duration_str or 'N/A'}) timed out after {ffmpeg_process_timeout}s.")
        if process and process.process and hasattr(process.process, 'kill'):
            try:
                process.process.kill()
                logger.info(f"Killed ffmpeg process {process.process.pid} due to timeout.")
            except Exception as kill_e:
                logger.error(f"Error killing ffmpeg process {process.process.pid}: {kill_e}")
        await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text=f"Download failed: The recording process (for {user_friendly_duration_str or 'unlimited'}) took too long and was stopped.", reply_markup=None)
    except Exception:
        logger.exception(f"Unexpected error during download execution for {url} (recording {user_friendly_duration_str or 'N/A'})")
        await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text=f"An unexpected error occurred during download (for {user_friendly_duration_str or 'unlimited'} recording).", reply_markup=None)

    # --- Upload Phase ---
    if download_success:
        upload_caption = f"Here is your video (recorded for {user_friendly_duration_str})." if user_friendly_duration_str and user_friendly_duration_str != "Unlimited" else "Here is your recorded video."
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text=f"Successfully recorded for {user_friendly_duration_str or 'unlimited'}. Now uploading video...", reply_markup=None)
            logger.info(f"Attempting to upload {current_output_filename} for chat_id {chat_id} (recorded for {user_friendly_duration_str or 'N/A'})")
            if not os.path.exists(current_output_filename) or os.path.getsize(current_output_filename) == 0:
                logger.error(f"Upload failed: {current_output_filename} is missing or empty.")
                await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text="Upload failed: Downloaded file is missing or empty.", reply_markup=None)
            else:
                with open(current_output_filename, 'rb') as video_file:
                    await context.bot.send_video(
                        chat_id=chat_id, video=video_file, filename=current_output_filename, caption=upload_caption,
                        connect_timeout=20, read_timeout=60, write_timeout=1800
                    )
                logger.info(f"Video {current_output_filename} uploaded successfully to chat_id {chat_id}!")
                await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text=f"Video (recorded for {user_friendly_duration_str or 'unlimited'}) uploaded successfully!", reply_markup=None)
        except FileNotFoundError:
            logger.error(f"Upload failed: {current_output_filename} not found for chat_id {chat_id}.")
            await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text="Upload failed: File not found (unexpected).", reply_markup=None)
        except TimedOut:
            logger.error(f"Telegram API timeout during upload of {current_output_filename} (recorded for {user_friendly_duration_str or 'N/A'}) to chat_id {chat_id}.")
            await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text="Upload failed: Connection to Telegram timed out.", reply_markup=None)
        except NetworkError as ne:
            logger.error(f"Telegram API network error during upload of {current_output_filename} (recorded for {user_friendly_duration_str or 'N/A'}) to chat_id {chat_id}: {ne}")
            await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text="Upload failed: Network error with Telegram.", reply_markup=None)
        except TelegramError as te:
            logger.exception(f"Telegram API error during upload of {current_output_filename} (recorded for {user_friendly_duration_str or 'N/A'}) to chat_id {chat_id}")
            await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text=f"Upload failed: Telegram error - {te.message}", reply_markup=None)
        except Exception:
            logger.exception(f"Unexpected error during upload of {current_output_filename} (recorded for {user_friendly_duration_str or 'N/A'}) to chat_id {chat_id}")
            await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text="Upload failed: An unexpected server error.", reply_markup=None)

    # --- Cleanup Phase ---
    if os.path.exists(current_output_filename):
        try:
            logger.info(f"Cleaning up file: {current_output_filename}")
            os.remove(current_output_filename)
        except OSError:
            logger.exception(f"Error deleting file {current_output_filename}")

async def record_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /record command."""
    # This is the function to be modified for the current subtask.
    # The version above is the one before this subtask's changes.
    # I will apply the new logic for duration selection here in the next step.
    if not update.message or not update.message.text or not update.effective_user or not update.effective_chat:
        logger.warning("record_video: Essential update attributes missing.")
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    logger.info(f"Received /record command from user_id {user_id} in chat_id {chat_id} with args: {context.args}")

    if not context.args:
        logger.info(f"User {user_id} sent /record without URL.")
        await update.message.reply_text("Please provide a URL after the command. Usage: /record <M3U8_URL>")
        return

    url = context.args[0]
    if not (url.startswith("http://") or url.startswith("https://")) or not (".m3u8" in url):
        logger.info(f"User {user_id} provided invalid URL: {url}")
        await update.message.reply_text(
            "The URL provided does not seem to be a valid M3U8 link. "
            "Please ensure it starts with http:// or https:// and contains '.m3u8'.\n"
            "Example: /record https://example.com/stream.m3u8"
        )
        return

    # Current subtask: Instead of calling download_video, show duration buttons.
    logger.info(f"User {user_id} provided valid URL: {url}. Prompting for duration.")

    keyboard = [
        [
            InlineKeyboardButton("10 seconds", callback_data="10s"),
            InlineKeyboardButton("5 minutes", callback_data="5m"),
        ],
        [
            InlineKeyboardButton("30 minutes", callback_data="30m"),
            InlineKeyboardButton("1 hour", callback_data="1h"),
        ],
        [InlineKeyboardButton("Unlimited", callback_data="unlimited")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        escaped_url = html.escape(url)
        prompt_message = await update.message.reply_html(
            f"Please select recording duration for:\n<code>{escaped_url}</code>",
            reply_markup=reply_markup
        )
        if not context.user_data: context.user_data = {}
        context.user_data[str(prompt_message.message_id)] = {'url': url, 'chat_id': chat_id, 'user_id': user_id}
        logger.info(f"Duration prompt sent for URL {url} to user {user_id}, prompt_message_id {prompt_message.message_id}. Stored URL in user_data.")
    except TelegramError as e:
        logger.exception(f"Failed to send duration prompt to user {user_id} for URL {url}")
        await update.message.reply_text("Sorry, I couldn't ask for the duration. Please try again.")
    except Exception as e:
        logger.exception(f"Unexpected error in record_video for user {user_id}, URL {url}")
        await update.message.reply_text("An unexpected error occurred. Please try the command again.")


async def duration_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles button presses for recording duration."""
    query = update.callback_query
    if not query or not query.data or not query.message: # Ensure query and message exist
        logger.warning("duration_button_callback received incomplete update.")
        if query: await query.answer(text="Error: Could not process request.")
        return

    await query.answer() # Acknowledge the button press

    user_id = query.from_user.id
    original_message_id_str = str(query.message.message_id)
    chat_id = query.message.chat.id

    logger.info(f"Callback received: data='{query.data}', user_id={user_id}, message_id={original_message_id_str}, chat_id={chat_id}")

    task_details = context.user_data.get(original_message_id_str)

    if not task_details:
        logger.warning(f"No URL found in user_data for message_id {original_message_id_str}. User {user_id}. It might be an old message or data was cleared.")
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=query.message.message_id, # Edit the message with buttons
            text="This recording request has expired or the data was lost. Please try the `/record` command again.",
            reply_markup=None # Remove keyboard
        )
        return

    # Clean up user_data immediately after retrieving
    try:
        del context.user_data[original_message_id_str]
        logger.info(f"Removed task details from user_data for message_id {original_message_id_str}")
    except KeyError:
        logger.warning(f"Tried to delete task details for message_id {original_message_id_str}, but key was already gone.")


    url = task_details.get('url')
    if not url:
        logger.error(f"URL missing in task_details for message_id {original_message_id_str}. Data: {task_details}")
        await context.bot.edit_message_text("Error: Original URL not found. Please try again.", chat_id=chat_id, message_id=query.message.message_id, reply_markup=None)
        return

    selected_duration_str = query.data
    duration_seconds = 0
    user_friendly_duration_str = "Unlimited" # Default for logging and messages

    if selected_duration_str == "10s":
        duration_seconds = 10
        user_friendly_duration_str = "10 seconds"
    elif selected_duration_str == "5m":
        duration_seconds = 5 * 60
        user_friendly_duration_str = "5 minutes"
    elif selected_duration_str == "30m":
        duration_seconds = 30 * 60
        user_friendly_duration_str = "30 minutes"
    elif selected_duration_str == "1h":
        duration_seconds = 60 * 60
        user_friendly_duration_str = "1 hour"
    elif selected_duration_str == "unlimited":
        duration_seconds = 0
        user_friendly_duration_str = "Unlimited" # Already set, but explicit
    else:
        logger.warning(f"Unknown duration callback data received: {selected_duration_str}")
        await context.bot.edit_message_text(f"Error: Invalid duration '{selected_duration_str}' selected. Please try `/record` again.", chat_id=chat_id, message_id=query.message.message_id, reply_markup=None)
        return

    try:
        await context.bot.edit_message_text(
            text=f"Duration selected: {user_friendly_duration_str}.\nPreparing to record from:\n<code>{html.escape(url)}</code>",
            chat_id=chat_id,
            message_id=query.message.message_id,
            reply_markup=None,
            parse_mode='HTML'
        )
    except TelegramError as e:
        logger.error(f"Error editing message after duration selection: {e}")

    logger.info(f"Calling download_video for URL: {url}, User: {user_id}, Duration: {duration_seconds}s ({user_friendly_duration_str}), Original Msg ID: {query.message.message_id}")

    await download_video(url=url, update=update, context=context,
                         duration_limit_seconds=duration_seconds,
                         original_message_id=query.message.message_id,
                         user_friendly_duration_str=user_friendly_duration_str)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        logger.warning("start: effective_user or message is None.")
        return
    user = update.effective_user
    logger.info(f"User {user.id} ({user.first_name}) started the bot.")
    welcome_message = (
        f"Hello {user.first_name}!\n\n"
        "I am your friendly video utility bot.\n"
        "You can use the following commands:\n"
        "/start - Show this welcome message\n"
        "/help - Show help information\n"
        "/record &lt;url&gt; - Download a video from an M3U8 URL. You'll be asked to select the recording duration (e.g., 10s, 5m, 1h, unlimited). (Max 2GB upload).\n"
    )
    try: await update.message.reply_html(welcome_message)
    except TelegramError: logger.exception(f"Failed to send welcome message to user {user.id}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        logger.warning("help_command: effective_user or message is None.")
        return
    user_id = update.effective_user.id
    logger.info(f"User {user_id} requested /help.")
    help_text = (
        "Here's how to use the bot:\n\n"
        "**Available commands:**\n"
        "🔹 `/start` - Show the welcome message.\n"
        "🔹 `/help` - Show this help message.\n"
        "🔹 `/record <M3U8_URL>` - Download a video from an M3U8 URL. After sending the command, you will be prompted to select a recording duration (e.g., 10 seconds, 5 minutes, 1 hour, or unlimited). The bot will show progress, send the video file (up to 2GB), and then delete it from the server.\n\n"
        "**Example Usage:**\n"
        "`/record https://example.com/live/stream.m3u8` (You will then be prompted for duration.)\n\n"
        "**Notes:**\n"
        "▪️ Video processing and uploading can take some time depending on the video size and selected duration.\n"
        "▪️ Ensure the link is a direct M3U8 link."
    )
    try: await update.message.reply_text(help_text, parse_mode="Markdown")
    except TelegramError: logger.exception(f"Failed to send help message to user {user_id}")

def main() -> None:
    logger.info("Starting bot application...")
    if TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        logger.critical("BOT TOKEN IS NOT SET!")
        return
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("record", record_video))

    # Define a pattern that matches the callback_data from duration buttons
    # This makes the handler more specific.
    duration_callback_pattern = "^(" + "|".join(["10s", "5m", "30m", "1h", "unlimited"]) + ")$"
    application.add_handler(CallbackQueryHandler(duration_button_callback, pattern=duration_callback_pattern))

    logger.info("Bot handlers registered. Starting polling...")
    try: application.run_polling()
    except Exception: logger.critical("Bot polling failed critically", exc_info=True)
    finally: logger.info("Bot application shutting down.")

if __name__ == "__main__":
    main()
