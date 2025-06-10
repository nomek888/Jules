import logging
import os
import re
import asyncio
import ffmpeg # type: ignore

from telegram import Update
from telegram.error import TelegramError, NetworkError, TimedOut # Added NetworkError, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes

# Enable logging - This is correctly placed at the module level.
# The level is INFO. For more detailed ffmpeg output parsing or PTB debug logs, DEBUG might be needed.
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# Get a logger instance for this module
logger = logging.getLogger(__name__)

TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
OUTPUT_FILENAME = "video.mp4"

async def read_progress(stderr_pipe, total_duration_ms: float, update: Update, context: ContextTypes.DEFAULT_TYPE, progress_message_id: int):
    """Reads ffmpeg progress from stderr and updates the message."""
    logger.debug(f"read_progress started for message_id {progress_message_id}. Total duration: {total_duration_ms}ms")
    last_reported_percentage = -1

    if stderr_pipe is None:
        logger.warning("stderr_pipe is None in read_progress. Cannot read progress.")
        return

    async for line_bytes in stderr_pipe:
        line = line_bytes.decode('utf-8', errors='ignore') # Add errors='ignore' for robustness
        logger.debug(f"ffmpeg stderr: {line.strip()}")

        current_ms = None
        time_match = re.search(r"out_time_ms=(\d+)", line)
        if time_match:
            current_ms = int(time_match.group(1)) / 1000  # microseconds to milliseconds
        else:
            time_match_hhmmss = re.search(r"time=(\d{2}):(\d{2}):(\d{2})\.(\d{2,3})", line)
            if time_match_hhmmss:
                hours, minutes, seconds, ms_fraction = map(int, time_match_hhmmss.groups())
                # ms_fraction can be 2 digits (centiseconds) or 3 digits (milliseconds)
                current_ms = (hours * 3600 + minutes * 60 + seconds) * 1000 + \
                             ms_fraction * (10 if len(str(ms_fraction)) == 2 else 1)

        if current_ms is not None and total_duration_ms > 0:
            percentage = int((current_ms / total_duration_ms) * 100)
            if percentage > last_reported_percentage and (percentage % 5 == 0 or percentage >= 99): # Update every 5% or near end
                try:
                    logger.debug(f"Updating progress for message_id {progress_message_id}: {percentage}%")
                    await context.bot.edit_message_text(
                        chat_id=update.effective_chat.id,
                        message_id=progress_message_id,
                        text=f"Downloading: {percentage}%"
                    )
                    last_reported_percentage = percentage
                except TelegramError as e: # More specific catch
                    logger.warning(f"TelegramError updating progress message for message_id {progress_message_id}: {e}")
                except Exception as e:
                    logger.exception(f"Unexpected error updating progress message for message_id {progress_message_id}")

        if "progress=end" in line:
            logger.info(f"ffmpeg progress indicated end for message_id {progress_message_id}.")
            break
    logger.debug(f"read_progress finished for message_id {progress_message_id}.")


async def download_video(url: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Downloads video from M3U8 URL, tracks progress, and uploads it."""
    if not update.message or not update.effective_chat:
        logger.warning("download_video called with no message or effective_chat.")
        return

    chat_id = update.effective_chat.id
    # Send initial message and get its ID for future edits
    try:
        progress_message = await update.message.reply_text("Preparing to download...")
        progress_message_id = progress_message.message_id
    except TelegramError as e:
        logger.exception(f"Failed to send initial progress message to chat_id {chat_id}")
        # No progress_message_id, so can't update user further.
        return

    total_duration_ms = 0.0
    download_success = False

    # --- Probing Phase ---
    try:
        logger.info(f"Probing URL: {url} for chat_id {chat_id}, message_id {progress_message_id}")
        await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text="Inspecting video stream...")

        probe_info = await asyncio.to_thread(ffmpeg.probe, url, timeout=30) # Added timeout to probe

        if 'format' in probe_info and 'duration' in probe_info['format']:
            duration_str = probe_info['format']['duration']
            if duration_str and duration_str != "N/A":
                total_duration_ms = float(duration_str) * 1000
                logger.info(f"Video duration: {total_duration_ms / 1000}s for {url}")
            else:
                logger.warning(f"Video duration reported as N/A or empty for {url}.")

        if total_duration_ms <= 0: # If duration couldn't be determined or is zero
            await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text="Could not determine video duration. Progress updates might be unavailable.")
            logger.warning(f"Could not determine valid video duration for {url}.")

    except ffmpeg.Error as e:
        stderr_output = e.stderr.decode('utf8', errors='ignore') if e.stderr else "No stderr."
        logger.error(f"ffmpeg probe error for {url}: {stderr_output}")
        user_message = f"Error inspecting video stream. The URL might be invalid or the stream format is not supported.\nDetails: {stderr_output[:200]}"
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text=user_message)
        except TelegramError:
            logger.warning(f"Failed to send probe error message to chat_id {chat_id}")
        return
    except asyncio.TimeoutError: # Specific catch for probe timeout
        logger.error(f"ffmpeg probe timed out for {url}")
        user_message = "Error inspecting video stream: The server took too long to respond. Please try again later."
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text=user_message)
        except TelegramError:
            logger.warning(f"Failed to send probe timeout message to chat_id {chat_id}")
        return
    except Exception as e: # Catch other unexpected errors during probing
        logger.exception(f"Unexpected error during probing of {url}")
        user_message = "An unexpected error occurred while trying to inspect the video. Please try again."
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text=user_message)
        except TelegramError:
            logger.warning(f"Failed to send unexpected probe error message to chat_id {chat_id}")
        return

    # --- Download Phase ---
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text="Downloading: 0%")

        if os.path.exists(OUTPUT_FILENAME):
            logger.info(f"Removing existing file: {OUTPUT_FILENAME} before download.")
            os.remove(OUTPUT_FILENAME)

        logger.info(f"Starting ffmpeg download for {url} to {OUTPUT_FILENAME}")
        stream = ffmpeg.input(url, format='hls', live_start_index='-3') # Added some M3U8 specific options
        stream = ffmpeg.output(stream, OUTPUT_FILENAME, c='copy', progress='pipe:1')
        # Using asyncio.to_thread for run_async as it's a blocking call internally
        process = await asyncio.to_thread(
            stream.run_async, pipe_stdin=False, pipe_stdout=False, pipe_stderr=True, quiet=True # quiet=True to suppress ffmpeg console output if not needed for debug
        )

        logger.info(f"ffmpeg process started for {url}. PID: {process.process.pid if process.process else 'N/A'}. Reading progress...")
        if process.stderr:
            await read_progress(process.stderr, total_duration_ms, update, context, progress_message_id)

        # Wait for the process to complete, with a timeout (e.g. 2 hours for very large files)
        # This timeout is for the entire ffmpeg process.
        ffmpeg_timeout = 7200 # 2 hours
        await asyncio.wait_for(asyncio.shield(process.wait()), timeout=ffmpeg_timeout)
        return_code = process.process.returncode if process.process else -1


        if return_code == 0:
            logger.info(f"Video downloaded successfully: {OUTPUT_FILENAME} from {url}")
            download_success = True
        else:
            # Stderr might have been consumed by read_progress or might be empty.
            # ffmpeg.Error usually captures stderr well if run_async itself throws it.
            logger.error(f"ffmpeg download process for {url} failed. Return code: {return_code}.")
            user_message = "Download failed. The video stream might have ended abruptly or there was a network issue."
            await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text=user_message)

    except ffmpeg.Error as e: # Error from ffmpeg command itself
        stderr_output = e.stderr.decode('utf8', errors='ignore') if e.stderr else "No stderr."
        logger.error(f"ffmpeg run error for {url}: {stderr_output}")
        user_message = f"Error during video download process. Details: {stderr_output[:200]}"
        await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text=user_message)
    except asyncio.TimeoutError: # Timeout for the ffmpeg process itself
        logger.error(f"ffmpeg download process for {url} timed out after {ffmpeg_timeout}s.")
        user_message = "Download failed: The process took too long and was stopped."
        if process and process.process: process.process.kill() # Ensure ffmpeg is killed
        await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text=user_message)
    except Exception as e: # Catch other unexpected errors during download
        logger.exception(f"Unexpected error during download execution for {url}")
        user_message = "An unexpected error occurred during the download. Please try again."
        await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text=user_message)

    # --- Upload Phase ---
    if download_success:
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text="Download complete! Now uploading video...")
            logger.info(f"Attempting to upload {OUTPUT_FILENAME} for chat_id {chat_id}")

            if not os.path.exists(OUTPUT_FILENAME) or os.path.getsize(OUTPUT_FILENAME) == 0:
                logger.error(f"Upload failed: {OUTPUT_FILENAME} is missing or empty after supposedly successful download.")
                await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text="Upload failed: The downloaded file is missing or empty.")
            else:
                with open(OUTPUT_FILENAME, 'rb') as video_file:
                    await context.bot.send_video(
                        chat_id=chat_id,
                        video=video_file,
                        filename=OUTPUT_FILENAME,
                        caption="Here is your recorded video!",
                        connect_timeout=20, # Increased connect timeout
                        read_timeout=60,    # Increased read timeout for server response
                        write_timeout=1800  # 30 minutes, more generous for 2GB
                    )
                logger.info(f"Video {OUTPUT_FILENAME} uploaded successfully to chat_id {chat_id}!")
                await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text="Video uploaded successfully!")

        except FileNotFoundError: # Should be caught by the os.path.exists check above
            logger.error(f"Upload failed: {OUTPUT_FILENAME} not found for chat_id {chat_id}.")
            await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text="Upload failed: The downloaded file was not found. This is unexpected.")
        except TimedOut: # More specific TelegramError
            logger.error(f"Telegram API timeout during upload of {OUTPUT_FILENAME} to chat_id {chat_id}.")
            await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text="Upload failed: The connection to Telegram timed out. Please try again later.")
        except NetworkError as ne: # More specific TelegramError for network issues
            logger.error(f"Telegram API network error during upload of {OUTPUT_FILENAME} to chat_id {chat_id}: {ne}")
            await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text="Upload failed: A network error occurred while communicating with Telegram. Please check your connection or try again later.")
        except TelegramError as te: # General Telegram errors
            logger.exception(f"Telegram API error during upload of {OUTPUT_FILENAME} to chat_id {chat_id}")
            await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text=f"Upload failed due to a Telegram error: {te.message}")
        except Exception as e: # Catch other unexpected errors during upload
            logger.exception(f"Unexpected error during upload of {OUTPUT_FILENAME} to chat_id {chat_id}")
            await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_message_id, text="Upload failed due to an unexpected server error. Please try again.")

    # --- Cleanup Phase ---
    if os.path.exists(OUTPUT_FILENAME):
        try:
            logger.info(f"Cleaning up file: {OUTPUT_FILENAME}")
            os.remove(OUTPUT_FILENAME)
        except OSError as e:
            logger.exception(f"Error deleting file {OUTPUT_FILENAME}")
            # User typically doesn't need to be notified about cleanup issues unless critical


async def record_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /record command to download a video."""
    if not update.message or not update.message.text or not update.effective_user:
        logger.warning("record_video: Update, message, text, or user is None.")
        return

    user_id = update.effective_user.id
    logger.info(f"Received /record command from user_id {user_id} with args: {context.args}")

    if not context.args:
        logger.info(f"User {user_id} sent /record without URL.")
        await update.message.reply_text("Please provide a URL after the command. Usage: /record <M3U8_URL>")
        return

    url = context.args[0]
    # Improved URL validation to be a bit more robust, though not perfect
    if not (url.startswith("http://") or url.startswith("https://")) or not (".m3u8" in url):
        logger.info(f"User {user_id} provided invalid URL: {url}")
        await update.message.reply_text(
            "The URL provided does not seem to be a valid M3U8 link. "
            "Please ensure it starts with http:// or https:// and contains '.m3u8'.\n"
            "Example: /record https://example.com/stream.m3u8"
        )
        return

    logger.info(f"User {user_id} calling download_video for URL: {url}")
    try:
        await download_video(url, update, context)
    except Exception as e:
        logger.exception(f"Unhandled exception in record_video for user {user_id}, URL {url}")
        await update.message.reply_text("An unexpected critical error occurred. The developers have been notified.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message when the /start command is issued."""
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
        "/record &lt;url&gt; - Download a video from an M3U8 URL (up to 2GB).\n"
    )
    try:
        await update.message.reply_html(welcome_message)
    except TelegramError as e:
        logger.exception(f"Failed to send welcome message to user {user.id}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends help information when the /help command is issued."""
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
        "🔹 `/record <M3U8_URL>` - Download a video from an M3U8 URL. The bot will show progress, send the video file (up to 2GB), and then delete it from the server.\n\n"
        "**Example Usage:**\n"
        "`/record https://example.com/live/stream.m3u8`\n\n"
        "**Notes:**\n"
        "▪️ Video processing and uploading can take some time depending on the video size and network conditions.\n"
        "▪️ Ensure the link is a direct M3U8 link."
    )
    try:
        await update.message.reply_text(help_text, parse_mode="Markdown")
    except TelegramError as e:
        logger.exception(f"Failed to send help message to user {user_id}")

def main() -> None:
    """Start the bot."""
    # Logging is configured at the top of the file.
    logger.info("Starting bot application...")

    # It's good practice to ensure TOKEN is set.
    if TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        logger.critical("BOT TOKEN IS NOT SET! Please replace 'YOUR_TELEGRAM_BOT_TOKEN' with your actual bot token.")
        return

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("record", record_video))

    logger.info("Bot handlers registered. Starting polling...")
    try:
        application.run_polling()
    except Exception as e: # Catch errors during bot startup/polling
        logger.critical("Bot polling failed critically", exc_info=True)
    finally:
        logger.info("Bot application shutting down.")

if __name__ == "__main__":
    main()
